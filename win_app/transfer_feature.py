"""Sending files and apps to the other PC, wired into the app: the transfer listener
(core/transfer.py), the outbox (send_win.py), what arrives (launch_win.py), the strip a drag at a
crossing edge shows, and Explorer's "Open on <the other PC>" entry.

A drag that reaches a crossing edge carries on over there (`carry`, the default): this PC cancels
its own drag, the pointer crosses at the same height, the files go across, and the other PC starts
the same drag under its pointer, so they are dropped wherever the button is let go there. An app
shortcut dragged across starts the other PC's own copy, or this PC's when the other has none.
With `carry` off the strip is a drop target instead: what is dropped on it opens over there.

Kept apart from kvm_bridge_win.py, which only creates a TransferFeature and calls start(), stop(),
add_to_tray() and send(), so the fork stays easy to bring up to date with Beamer itself.
"""

import logging
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QMimeData, QObject, QPoint, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QFontMetrics, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QFileDialog, QWidget

import launch_win
import send_win
from core import protocol, return_edge, transfer
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
# A carried drag: how often this PC checks for the button being let go, how long the other PC
# waits after pressing its button before starting the drag, and how long a drag may last before
# its button is let go regardless (a lost release must not leave the other PC's button down).
CARRY_POLL_MS = 25
CARRY_PRESS_MS = 60
CARRY_TIMEOUT_MS = 120_000
# More than this is not carried in the hand: over Wi-Fi it takes longer than a drag lasts.
CARRY_MAX_BYTES = 256 * 1024 * 1024


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


def bulky(paths, excludes=(), limit=CARRY_MAX_BYTES):
    """Whether `paths` hold more than `limit` bytes, the folders `excludes` names left out as a
    send leaves them. Stops counting as soon as the answer is known, so a big tree is not walked."""
    total, skip = 0, set(excludes)
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, names in os.walk(path):
                dirs[:] = [name for name in dirs if name not in skip]
                for name in names:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        continue
                    if total > limit:
                        return True
        else:
            try:
                total += os.path.getsize(path)
            except OSError:
                continue
        if total > limit:
            return True
    return False


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


def _drag_pixmap(text):
    """What a carried drag shows under the pointer: the names it carries, on a small card."""
    font = QFont()
    font.setBold(True)
    metrics = QFontMetrics(font)
    label = metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, 320)
    pixmap = QPixmap(metrics.horizontalAdvance(label) + 24, metrics.height() + 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(46, 125, 255, 220))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(pixmap.rect(), 8, 8)
    painter.setPen(QColor(255, 255, 255))
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, label)
    painter.end()
    return pixmap


def local_paths(mime):
    if mime is None or not mime.hasUrls():
        return []
    return [url.toLocalFile() for url in mime.urls() if url.isLocalFile() and url.toLocalFile()]


class DropZone(QWidget):
    """A strip along the crossing edge, shown while something is dragged into it. Dropping on it
    sends what was dragged to the machine beyond that edge. It never takes focus, and it goes
    away as soon as the mouse button is up."""

    dropped = Signal(list, object)
    # (paths, peer, edge): a drag of files entered the strip and is to be carried across.
    entered = Signal(list, object, str)

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
        # Carry the drag across rather than wait for a drop (TransferSettings.carry).
        self.carry = True
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
        paths = local_paths(event.mimeData())
        if paths and self.carry:
            # Not a drop target: the drag is ended here and carried on over there.
            event.ignore()
            peer, edge = self._peer, self._edge
            self.withdraw()
            self.entered.emit(paths, peer, edge)
            return
        if paths:
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
        # Every pixel gets some alpha: Windows passes a drag straight through a translucent
        # window's fully transparent pixels, and a drag pushing at the edge sits on the last one.
        painter.fillRect(self.rect(), QColor(46, 125, 255, 60))
        fill = QColor(46, 125, 255, 200 if self._hover else 120)
        painter.setPen(QPen(QColor(255, 255, 255, 220), 2))
        painter.setBrush(fill)
        painter.drawRoundedRect(self.rect().adjusted(3, 3, -3, -3), 12, 12)
        painter.setPen(QColor(255, 255, 255))
        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        text = (tr("To {name}").format(name=self._name) if self.carry
                else tr("Drop to open on {name}").format(name=self._name))
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
    # Carrying a drag: files dragged here arrived (entry, folder, offer); the button that drags
    # them was let go on the other PC (job id); an app launched over there, so cross (peer, edge).
    carry_arrived = Signal(object, str, object)
    carry_released = Signal(str)
    cross_to = Signal(object, str)

    def __init__(self, app, button_down=None, injector=None, desktop=None):
        super().__init__(app)
        self._app = app
        if button_down is None or injector is None or desktop is None:
            import platform_parts
            injector = injector or platform_parts.injector
            desktop = desktop or platform_parts.desktop
            if button_down is None:
                button_down = lambda: getattr(desktop, "button_down", lambda _name: False)("left")
        self._button_down = button_down
        self._injector = injector
        self._desktop = desktop
        self.receiver = launch_win.Receiver(self.settings, notify=self.notice.emit, carry=self._carry_from_server,
                                            notice=self._notice_from_server, ask_back=self._ask_back)
        self.server = transfer.TransferServer(app.book, app._identity, self.receiver.inbox, self.receiver.decide,
                                              self.receiver.received, self.receiver.launch, notice=self.receiver.notice)
        self.outbox = send_win.Outbox(self._peers, self.settings, lambda: bytes(app._identity()["id"]), self.report.emit)
        self.zone = DropZone(button_down)
        self.zone.dropped.connect(self._on_dropped)
        self.zone.entered.connect(self._on_entered)
        self.report.connect(self._on_report)
        # Bound methods of this QObject, so emits from the serving and hook threads are queued
        # onto the GUI thread.
        self.notice.connect(self._on_notice)
        self.drag.connect(self._on_drag)
        self.carry_arrived.connect(self._on_carry_arrived)
        self.carry_released.connect(self._on_carry_released)
        self.cross_to.connect(self._cross)
        app.sender.drag_at_edge = self._drag_from_hook
        self._last_drag = (None, None, 0.0)
        self._listening = False
        self._port_failed = False
        self._announced = None
        self._settings_cache = (None, None)
        # Sending side of a carry: the job whose button is being watched, and for whom.
        self._release_timer = QTimer(self)
        self._release_timer.setInterval(CARRY_POLL_MS)
        self._release_timer.timeout.connect(self._poll_release)
        self._outgoing = None
        self._release_started = 0.0
        # Receiving side: the job being dragged here, and jobs whose button was let go first.
        self._dragging = None
        self._released = set()

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
        launch_win._startfile(inbox)

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
        settings = self.settings()
        if self.zone.showing(peer, edge) or not settings.drop_zone or not isinstance(peer, bytes):
            return
        target = send_win.pick_target(self._peers(), protocol.id_text(peer))
        if target is None:
            return
        self.zone.carry = settings.carry
        for screen in QGuiApplication.screens():
            if native_geometry(screen).contains(QPoint(x, y)):
                self.zone.offer(peer, edge, target.get("name") or tr("the other PC"), screen, zone_rect(screen.geometry(), edge))
                return

    # Carrying a drag: the sending side

    def _on_entered(self, paths, peer, edge):
        """A drag of `paths` reached the strip at `edge`: end it here, and carry it to `peer`."""
        if not isinstance(peer, bytes):
            return
        target_id = protocol.id_text(peer)
        # Cancel this PC's own drag (Explorer's) before the pointer leaves, so letting the button
        # go later drops nothing here. Injected input carries Beamer's mark: the hook passes it.
        try:
            self._injector.inject_key("esc", True)
            self._injector.inject_key("esc", False)
        except Exception:
            LOGGER.exception("Could not cancel this PC's own drag")
        files, launches = [], []
        for path in paths:
            more_files, apps = send_win.plan([path])
            files.extend(more_files)
            launches.extend((path, app) for app in apps)
        LOGGER.info("Carrying a drag across the %s edge: %d file(s), %d app(s)", edge, len(files), len(launches))
        for original, app in launches:
            # The other PC's own copy if it has one, else this PC's: cross only when it opened there.
            self.outbox.launch_there_or_here(
                original, app, target_id,
                done=lambda where, peer=peer, edge=edge: self.cross_to.emit(peer, edge) if where == "there" and not files else None)
        if not files:
            return
        if bulky(files, self.settings().excludes):
            # Too much to arrive while the button is still held: sent like a plain drop instead,
            # opened over there when it is all there.
            LOGGER.info("Too large to carry in the hand; sending it in the background instead")
            name = (send_win.pick_target(self._peers(), target_id) or {}).get("name") or tr("the other PC")
            self._app._on_alert("Beamer", tr("Too large to carry across: sending it to {name} in the background; it opens there when it arrives").format(name=name))
            self.outbox.submit(files, target_id)
            return
        job = send_win.new_job()
        self._outgoing = (job, target_id)
        self._cross(peer, edge)
        self.outbox.submit(files, target_id, carry=job)
        self._release_started = time.monotonic()
        self._release_timer.start()

    def _cross(self, peer, edge):
        """Move the keyboard and mouse to `peer` across `edge`, at the height the pointer is at."""
        offset = None
        try:
            bounds = return_edge.union(self._desktop.monitors())
            offset = return_edge.edge_offset(bounds, edge, self._desktop.cursor_position())
        except Exception:
            LOGGER.info("Crossing with a carried drag at the edge's middle", exc_info=True)
        try:
            self._app.sender.go(peer, return_edge.OPPOSITE[edge], offset)
        except Exception:
            LOGGER.exception("Could not cross with a carried drag")

    def _poll_release(self):
        """Watch this PC's button while its drag is carried: the moment it is let go, tell the
        other PC, which lets its own drag drop where its pointer is."""
        if self._outgoing is None:
            self._release_timer.stop()
            return
        timed_out = time.monotonic() - self._release_started > CARRY_TIMEOUT_MS / 1000
        try:
            down = self._button_down()
        except Exception:
            down = False
        if down and not timed_out:
            return
        self._release_timer.stop()
        job, target_id = self._outgoing
        self._outgoing = None
        LOGGER.info("Carried drag let go%s; telling the other PC", " (timed out)" if timed_out else "")
        self.outbox.send_notice(transfer.KIND_RELEASE, job, target_id)

    # Carrying a drag: the receiving side

    def _carry_from_server(self, entry, folder, offer):
        """On the serving thread: files dragged here arrived; the drag goes on in the GUI."""
        self.carry_arrived.emit(entry, folder, offer)
        return True, ""

    def _notice_from_server(self, entry, offer):
        """On the serving thread: a release for a drag here, or an open_back for a job sent from here."""
        if offer["kind"] == transfer.KIND_RELEASE:
            self.carry_released.emit(offer["ref"])
            return True, ""
        # Only a job this PC sent to that very machine, and only once.
        files = self.outbox.take_sent_files(offer["ref"], entry.get("id"))
        if files is None:
            return False, "no such job was sent from this PC to that one"
        problems = []
        for path in files:
            try:
                if os.path.isdir(path) or not launch_win.runs_code(path):
                    launch_win._startfile(path)
                else:
                    # Never run code because another machine asked: show it, the user decides.
                    launch_win._spawn([launch_win.explorer_exe(), "/select,", path])
            except OSError as exc:
                problems.append(f"{os.path.basename(path)}: {exc}")
        self.notice.emit("Beamer", tr("{items}: no app for it on {name}, so it opened here").format(
            items=", ".join(os.path.basename(path) for path in files), name=entry.get("name") or tr("the other PC")))
        return (False, "; ".join(problems)) if problems else (True, "")

    def _ask_back(self, entry, offer):
        """Nothing here opens the job's files: ask the PC they came from to open its own copies."""
        self.outbox.send_notice(transfer.KIND_OPEN_BACK, offer["job"], entry.get("id") or "")
        return True, ""

    def _open_fallback(self, entry, folder, offer):
        """Open a carried job as a plain one would be (it was let go before it arrived, or dropped
        where nothing took it), off the GUI thread since it may ask the other PC to open it."""
        import threading
        threading.Thread(target=self.receiver.open_received, args=(entry, folder, offer),
                         name="Beamer-carry-open", daemon=True).start()

    def _on_carry_arrived(self, entry, folder, offer):
        job = offer["job"]
        driven = bool(getattr(self._app.server, "driven", False))
        if job in self._released or not driven or self._dragging is not None:
            LOGGER.info("Carried files arrived but the drag cannot go on (let go already: %s, driven: %s, "
                        "busy: %s); opening them instead", job in self._released, driven, self._dragging is not None)
            self._released.discard(job)
            self._open_fallback(entry, folder, offer)
            return
        paths = [os.path.join(folder, root) for root in offer["roots"]]
        self._dragging = job
        try:
            self._injector.inject_mouse_button("left", True)
        except Exception:
            LOGGER.exception("Could not press the button for a carried drag")
            self._dragging = None
            self._open_fallback(entry, folder, offer)
            return
        QTimer.singleShot(CARRY_TIMEOUT_MS, lambda job=job: self._let_go(job))
        QTimer.singleShot(CARRY_PRESS_MS, lambda: self._run_drag(entry, folder, offer, paths))

    def _run_drag(self, entry, folder, offer, paths):
        job = offer["job"]
        if self._dragging != job:
            self._open_fallback(entry, folder, offer)
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(_drag_pixmap(", ".join(offer["roots"])))
        LOGGER.info("Carried drag goes on here with %d item(s)", len(paths))
        try:
            # Runs until the button is let go: by the release from the other PC, or the timeout.
            action = drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.LinkAction, Qt.DropAction.CopyAction)
        finally:
            self._let_go(job)
        LOGGER.info("Carried drag ended: %s", action.name)
        if action == Qt.DropAction.IgnoreAction:
            # Dropped where nothing takes files: open them, as a plain send would.
            self._open_fallback(entry, folder, offer)

    def _on_carry_released(self, job):
        LOGGER.info("The other PC let go of the carried drag%s", "" if self._dragging == job else " before it began here")
        if self._dragging == job:
            self._let_go(job)
        else:
            self._released.add(job)

    def _let_go(self, job):
        if self._dragging != job:
            return
        self._dragging = None
        try:
            self._injector.inject_mouse_button("left", False)
        except Exception:
            LOGGER.exception("Could not let go of the button of a carried drag")

    def _on_report(self, kind, text, peer_id):
        if kind == "progress":
            self._app.tray.setToolTip(text)
            return
        self._app.tray.setToolTip(self._app._drive_status())
        if kind == "failed":
            LOGGER.warning("Transfer failed: %s", text)
        if kind == "carried":
            LOGGER.info("Carried files reached the other PC")
            return
        self._app._on_alert("Beamer", text)
        if kind == "done" and self.settings().follow:
            peer = protocol.read_id(peer_id)
            if peer is not None:
                try:
                    self._app.sender.go(peer)
                except Exception:
                    LOGGER.exception("Could not move the keyboard and mouse to the machine just sent to")
