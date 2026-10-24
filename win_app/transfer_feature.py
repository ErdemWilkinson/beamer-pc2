"""Sending files and apps to the other PC, wired into the app: the transfer listener
(core/transfer.py), the outbox (send_win.py), what arrives (launch_win.py), the drop zone a drag
at a crossing edge shows, and Explorer's "Open on <the other PC>" entry.

Kept apart from kvm_bridge_win.py, which only creates a TransferFeature and calls start(), stop(),
add_to_tray() and send(), so the fork stays easy to bring up to date with Beamer itself.
"""

import logging
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QFileDialog, QWidget

import launch_win
import send_win
from core import protocol, transfer
from core.i18n import tr

LOGGER = logging.getLogger("Beamer")

SHELL_KEY = "BeamerSend"
SHELL_CLASSES = ("*", "Directory")
SEND_ARGUMENT = "--send-to-peer"
ZONE_WIDTH = 56
# How often the zone checks that the drag that showed it is still going.
ZONE_POLL_MS = 120
# The least time between two reports of a drag at the same edge reaching the GUI thread.
DRAG_EVERY = 0.25


def send_command():
    """The command line Explorer runs for "Open on…", with %1 for the item: the built Beamer.exe,
    or pythonw and this folder's kvm_bridge_win.py when run from source."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {SEND_ARGUMENT} "%1"', sys.executable
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(pythonw if pythonw.is_file() else Path(sys.executable))
    script = Path(__file__).resolve().with_name("kvm_bridge_win.py")
    return f'"{interpreter}" "{script}" {SEND_ARGUMENT} "%1"', interpreter


def _shell_key(cls, *more):
    return "\\".join(("Software", "Classes", cls, "shell", SHELL_KEY) + more)


def register_shell_menu(label, command, icon, winreg=None):
    """Put "Open on…" on files' and folders' context menus, for this user only (no admin)."""
    if winreg is None:
        import winreg
    for cls in SHELL_CLASSES:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _shell_key(cls)) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, label)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
            winreg.SetValueEx(key, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _shell_key(cls, "command")) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)


def unregister_shell_menu(winreg=None):
    if winreg is None:
        try:
            import winreg
        except ImportError:
            return
    for cls in SHELL_CLASSES:
        for sub in (_shell_key(cls, "command"), _shell_key(cls)):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except OSError:
                pass


def native_geometry(screen):
    """A screen's rectangle in the physical pixels Windows reports the cursor in. Qt keeps each
    screen's origin native and scales only its size."""
    geometry, ratio = screen.geometry(), screen.devicePixelRatio()
    return QRect(geometry.x(), geometry.y(), round(geometry.width() * ratio), round(geometry.height() * ratio))


def zone_rect(geometry, edge, width=ZONE_WIDTH):
    """The strip of `geometry` (a screen's logical rectangle) along `edge` the zone covers."""
    if edge == "left":
        return QRect(geometry.x(), geometry.y(), width, geometry.height())
    if edge == "right":
        return QRect(geometry.x() + geometry.width() - width, geometry.y(), width, geometry.height())
    if edge == "top":
        return QRect(geometry.x(), geometry.y(), geometry.width(), width)
    return QRect(geometry.x(), geometry.y() + geometry.height() - width, geometry.width(), width)


def local_paths(mime):
    if mime is None or not mime.hasUrls():
        return []
    return [url.toLocalFile() for url in mime.urls() if url.isLocalFile() and url.toLocalFile()]


class DropZone(QWidget):
    """A strip along the crossing edge, shown while something is dragged into it. Dropping on it
    sends what was dragged to the machine beyond that edge. It never takes focus, and it goes
    away as soon as the mouse button is up."""

    dropped = Signal(list, object)

    def __init__(self, button_down):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self._button_down = button_down
        self._peer = None
        self._edge = "right"
        self._name = ""
        self._hover = False
        self._timer = QTimer(self)
        self._timer.setInterval(ZONE_POLL_MS)
        self._timer.timeout.connect(self._check)

    def showing(self, peer, edge):
        return self.isVisible() and self._peer == peer and self._edge == edge

    def offer(self, peer, edge, name, screen, rect):
        if self.showing(peer, edge):
            return
        self._peer, self._edge, self._name, self._hover = peer, edge, name, False
        # On the screen it belongs to before the geometry, so the logical rectangle is scaled by
        # that screen's ratio rather than the primary's.
        self.winId()
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
        self.setGeometry(rect)
        self.show()
        self.setGeometry(rect)
        self.raise_()
        self._timer.start()

    def withdraw(self):
        self._timer.stop()
        self._hover = False
        self.hide()

    def _check(self):
        try:
            down = self._button_down()
        except Exception:
            down = False
        if not down and not self._hover:
            self.withdraw()

    def dragEnterEvent(self, event):
        if local_paths(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self._hover = True
            self.update()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragLeaveEvent(self, event):
        self._hover = False
        self.update()

    def dropEvent(self, event):
        paths = local_paths(event.mimeData())
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        peer = self._peer
        self.withdraw()
        if paths:
            self.dropped.emit(paths, peer)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(46, 125, 255, 200 if self._hover else 120)
        painter.setPen(QPen(QColor(255, 255, 255, 220), 2))
        painter.setBrush(fill)
        painter.drawRoundedRect(self.rect().adjusted(3, 3, -3, -3), 12, 12)
        painter.setPen(QColor(255, 255, 255))
        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        text = tr("Drop to open on {name}").format(name=self._name)
        if self._edge in ("left", "right"):
            painter.translate(self.width() / 2, self.height() / 2)
            painter.rotate(-90 if self._edge == "right" else 90)
            painter.drawText(QRect(-self.height() // 2, -self.width() // 2, self.height(), self.width()),
                             Qt.AlignmentFlag.AlignCenter, text)
        else:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)


class TransferFeature(QObject):
    """The transfer side of one running Beamer. `app` is the WindowsApplication: its `book`,
    `_identity`, `config_path`, `sender`, `_on_alert` and `_peer_entries` are what this uses."""

    report = Signal(str, str, str)
    notice = Signal(str, str)
    drag = Signal(object, str, int, int)

    def __init__(self, app, button_down=None):
        super().__init__(app)
        self._app = app
        if button_down is None:
            import desktop_win
            button_down = lambda: desktop_win.button_down("left")
        self.receiver = launch_win.Receiver(self.settings, notify=self.notice.emit)
        self.server = transfer.TransferServer(app.book, app._identity, self.receiver.inbox, self.receiver.decide,
                                              self.receiver.received, self.receiver.launch)
        self.outbox = send_win.Outbox(self._peers, self.settings, lambda: bytes(app._identity()["id"]), self.report.emit)
        self.zone = DropZone(button_down)
        self.zone.dropped.connect(self._on_dropped)
        self.report.connect(self._on_report)
        # Bound methods of this QObject, so emits from the serving and hook threads are queued
        # onto the GUI thread.
        self.notice.connect(self._on_notice)
        self.drag.connect(self._on_drag)
        app.sender.drag_at_edge = self._drag_from_hook
        self._last_drag = (None, None, 0.0)
        self._listening = False
        self._port_failed = False
        self._announced = None
        self._settings_cache = (None, None)

    def settings(self):
        """transfer.json as it is now, read again only when the file changes: this is asked on
        every drag at an edge and from the sending and serving threads."""
        path = launch_win.settings_path(self._app.config_path)
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            stamp = None
        cached_stamp, cached = self._settings_cache
        if cached is None or cached_stamp != stamp:
            cached = launch_win.load_settings(self._app.config_path)
            self._settings_cache = (stamp, cached)
        return cached

    def _drag_from_hook(self, peer, edge, pointer):
        """On the hook thread, for every raw move of a drag at the edge: passed on at most every
        DRAG_EVERY seconds for the same edge, so the GUI thread is not flooded."""
        now = time.monotonic()
        last_peer, last_edge, last_at = self._last_drag
        if last_peer == peer and last_edge == edge and now - last_at < DRAG_EVERY:
            return
        self._last_drag = (peer, edge, now)
        self.drag.emit(peer, edge, int(pointer[0]), int(pointer[1]))

    def _on_notice(self, title, text):
        self._app._on_alert(title, text)

    def _peers(self):
        try:
            return self._app.book.peers()
        except Exception:
            LOGGER.exception("Could not read the peers for a transfer")
            return []

    def target_name(self):
        peer = send_win.pick_target(self._peers(), self.settings().target)
        return (peer or {}).get("name") or tr("the other PC")

    # Lifecycle

    def start(self):
        if not self._listening:
            try:
                self.server.start()
                self._listening = True
            except OSError as exc:
                LOGGER.error("Cannot listen for transfers on port %s: %s", self.server.port, exc)
                if not self._port_failed:
                    self._port_failed = True
                    self._app._on_alert("Beamer", tr("Another program is using port {port}: files cannot be sent to this PC").format(port=self.server.port))
        self.outbox.start()
        self._refresh_shell_menu()

    def stop(self):
        self.zone.withdraw()
        self.outbox.stop()
        if self._listening:
            self.server.stop()
            self._listening = False

    def _refresh_shell_menu(self):
        if sys.platform != "win32":
            return
        settings = self.settings()
        try:
            if not settings.menu:
                if self._announced is not None:
                    unregister_shell_menu()
                    self._announced = None
                return
            label = tr("Open on {name}").format(name=self.target_name())
            command, icon = send_command()
            if self._announced != (label, command):
                register_shell_menu(label, command, icon)
                self._announced = (label, command)
        except OSError:
            LOGGER.exception("Could not update Explorer's Open on entry")

    # Sending

    def send(self, paths):
        self.outbox.submit(paths)

    def choose_and_send(self):
        files, _ = QFileDialog.getOpenFileNames(self._app, tr("Send to {name}").format(name=self.target_name()))
        if files:
            self.send(files)

    def choose_folder_and_send(self):
        folder = QFileDialog.getExistingDirectory(self._app, tr("Send to {name}").format(name=self.target_name()))
        if folder:
            self.send([folder])

    def open_inbox(self):
        inbox = self.settings().inbox_path()
        os.makedirs(inbox, exist_ok=True)
        os.startfile(inbox)

    def add_to_tray(self, menu, before):
        """The tray's transfer actions, inserted above `before`."""
        files = menu.addAction(tr("Send files to the other PC…"))
        files.triggered.connect(lambda _checked=False: self.choose_and_send())
        folder = menu.addAction(tr("Send a folder to the other PC…"))
        folder.triggered.connect(lambda _checked=False: self.choose_folder_and_send())
        inbox = menu.addAction(tr("Open received files"))
        inbox.triggered.connect(lambda _checked=False: self.open_inbox())
        for action in (files, folder, inbox):
            menu.removeAction(action)
            menu.insertAction(before, action)
        menu.insertSeparator(before)

    def _on_dropped(self, paths, peer):
        # To the machine beyond the edge it was dropped on, whatever transfer.json's target says.
        self.outbox.submit(paths, protocol.id_text(peer) if isinstance(peer, bytes) else "")

    def _on_drag(self, peer, edge, x, y):
        if self.zone.showing(peer, edge) or not self.settings().drop_zone or not isinstance(peer, bytes):
            return
        target = send_win.pick_target(self._peers(), protocol.id_text(peer))
        if target is None:
            return
        for screen in QGuiApplication.screens():
            if native_geometry(screen).contains(QPoint(x, y)):
                self.zone.offer(peer, edge, target.get("name") or tr("the other PC"), screen, zone_rect(screen.geometry(), edge))
                return

    def _on_report(self, kind, text, peer_id):
        if kind == "progress":
            self._app.tray.setToolTip(text)
            return
        self._app.tray.setToolTip(self._app._drive_status())
        self._app._on_alert("Beamer", text)
        if kind == "done" and self.settings().follow:
            peer = protocol.read_id(peer_id)
            if peer is not None:
                try:
                    self._app.sender.go(peer)
                except Exception:
                    LOGGER.exception("Could not move the keyboard and mouse to the machine just sent to")
