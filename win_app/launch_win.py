"""What this PC does with a transfer (core/transfer.py) once it has arrived, and the settings for it.

Received files are opened here with this PC's own apps, on its own hardware: a folder in the chosen
editor (VS Code, Antigravity or a path), any other file with the app Windows associates with it. A
launch names an app only, never a path or a binary, and starts this PC's own copy of it, and only
when the name is in the allowed list. Nothing that arrived is ever run: an executable or a script
among the files is saved to the inbox and shown in Explorer, never opened.

The settings live in transfer.json beside settings.json, apart from the link's schema, so a Beamer
update never has to know about them.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core import transfer
from core.i18n import tr

LOGGER = logging.getLogger("Beamer")

SETTINGS_NAME = "transfer.json"

EDITORS = ("code", "antigravity", "custom")

# The only kinds of file opened when they arrive: ones whose usual apps show them and run nothing.
# Anything else (programs, scripts, shortcuts, installers, Office files with macros, web pages,
# themes, and every kind no one has thought of yet) is saved to the inbox and selected in Explorer.
# An allowlist, because a list of what Windows might run is never complete.
OPENABLE_SUFFIXES = frozenset({
    ".txt", ".md", ".log", ".csv", ".json", ".yaml", ".yml", ".toml",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".mp3", ".wav", ".mp4", ".mkv", ".mov",
    ".pdf", ".docx", ".xlsx", ".pptx", ".zip",
})
# Marks what arrived as from the internet (Mark of the Web), so Office's Protected View and
# SmartScreen treat it as they would a download.
ZONE_IDENTIFIER = "[ZoneTransfer]\r\nZoneId=3\r\n"
_BIDI_AND_CONTROL = {chr(code) for code in range(32)} | set("‎‏‪‫‬‭‮⁦⁧⁨⁩")

# Apps a launch may start until the user changes the list: the editors this fork is for, and two
# harmless ones. The value is the path to use; "" means look it up (App Paths, then the usual
# install folders, then PATH).
DEFAULT_APPS = {"Code.exe": "", "Antigravity.exe": "", "notepad.exe": "", "explorer.exe": ""}

_EDITOR_EXES = {"code": "Code.exe", "antigravity": "Antigravity.exe"}


def _local_appdata():
    return os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")


def default_inbox():
    return os.path.join(os.path.expanduser("~"), "Beamer Inbox")


@dataclass
class TransferSettings:
    # Receive and open what a machine that may drive this one sends.
    receive: bool = True
    inbox: str = ""
    editor: str = "code"
    editor_path: str = ""
    apps: dict = field(default_factory=lambda: dict(DEFAULT_APPS))
    excludes: list = field(default_factory=lambda: list(transfer.DEFAULT_EXCLUDES))
    max_bytes: int = 2 * 1024 * 1024 * 1024
    # After a drop or "Open on PC2", move the keyboard and mouse there too.
    follow: bool = True
    # Explorer's "Open on <the other PC>" entry for files and folders.
    menu: bool = True
    # The strip that shows at a crossing edge while something is dragged into it.
    drop_zone: bool = True
    # The machine sends go to, by id; the only paired one when unset.
    target: str = ""

    def inbox_path(self):
        return os.path.expandvars(self.inbox) if self.inbox else default_inbox()


def settings_path(config_path):
    return Path(config_path).with_name(SETTINGS_NAME)


def load_settings(config_path):
    """The settings, or the defaults for anything missing or of the wrong type. Never raises: a
    broken transfer.json must not stop the link."""
    settings = TransferSettings()
    try:
        raw = json.loads(settings_path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if not isinstance(raw, dict):
        return settings
    for name, default in asdict(TransferSettings()).items():
        value = raw.get(name)
        if value is None or type(value) is not type(default):
            continue
        if name == "editor" and value not in EDITORS:
            continue
        if name == "apps":
            value = {app: path for app, path in value.items() if isinstance(path, str)}
        if name == "excludes":
            value = [item for item in value if isinstance(item, str)]
        setattr(settings, name, value)
    return settings


def save_settings(config_path, settings):
    path = settings_path(config_path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


# Finding an app on this PC

def _app_paths(name):
    """The App Paths registration for `name` (HKCU, then HKLM), as Windows' own Run box finds it."""
    try:
        import winreg
    except ImportError:
        return ""
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{name}") as key:
                value, _ = winreg.QueryValueEx(key, None)
        except OSError:
            continue
        value = os.path.expandvars(str(value).strip('"'))
        if os.path.isfile(value):
            return value
    return ""


def _known_places(name):
    lower = name.lower()
    local = _local_appdata()
    program_files = [os.environ[var] for var in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(var)]
    if lower == "code.exe":
        return [os.path.join(local, "Programs", "Microsoft VS Code", "Code.exe")] + [os.path.join(base, "Microsoft VS Code", "Code.exe") for base in program_files]
    if lower == "antigravity.exe":
        return [os.path.join(local, "Programs", "Antigravity", "Antigravity.exe")] + [os.path.join(base, "Antigravity", "Antigravity.exe") for base in program_files]
    if lower in ("notepad.exe", "explorer.exe"):
        windows = os.environ.get("SystemRoot") or r"C:\Windows"
        return [os.path.join(windows, "System32", name), os.path.join(windows, name)]
    return []


def find_app(name, configured=""):
    """The path of this PC's own `name`, or "" when it is not installed."""
    if configured:
        path = os.path.expandvars(configured)
        return path if os.path.isfile(path) else ""
    found = _app_paths(name)
    if found:
        return found
    for place in _known_places(name):
        if os.path.isfile(place):
            return place
    which = shutil.which(name)
    found = os.path.abspath(which) if which else ""
    # PATH may hold a .cmd shim of the same name; only an executable itself is started. On Windows
    # which() looks in the current folder first, which is no place to find an app to start.
    if not found.lower().endswith(".exe") or os.path.normcase(os.path.dirname(found)) == os.path.normcase(os.getcwd()):
        return ""
    return found


def allowed_app(settings, name):
    """The allowed list's own spelling of `name` (matched without case) and its configured path,
    or None when the list does not hold it."""
    for app, path in settings.apps.items():
        if app.lower() == name.lower():
            return app, path
    return None


def editor_exe(settings, find=find_app):
    if settings.editor == "custom":
        path = os.path.expandvars(settings.editor_path)
        return path if path and os.path.isfile(path) else ""
    name = _EDITOR_EXES[settings.editor]
    allowed = allowed_app(settings, name)
    return find(name, allowed[1] if allowed else "")


# What the receiver does

def _spawn(argv):
    """Start `argv` detached from Beamer, so closing Beamer leaves it running."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(argv, close_fds=True, creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _startfile(path):
    # The associated app, as a double-click would choose.
    os.startfile(path)


def openable(path):
    return Path(path).suffix.lower() in OPENABLE_SUFFIXES


def explorer_exe():
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "explorer.exe")


def plain(text):
    """`text` without control or bidirectional-override characters, which could make a name in a
    notice read as something it is not (an .exe shown as a .txt)."""
    return "".join(char for char in str(text) if char not in _BIDI_AND_CONTROL)


def mark_from_internet(folder):
    """Give every file under `folder` a Zone.Identifier stream. NTFS only, and never fatal."""
    if sys.platform != "win32":
        return
    for parent, _dirs, files in os.walk(folder):
        for name in files:
            try:
                with open(os.path.join(parent, name) + ":Zone.Identifier", "w", encoding="ascii") as stream:
                    stream.write(ZONE_IDENTIFIER)
            except OSError:
                return


class Receiver:
    """The callbacks core/transfer.TransferServer takes, under the settings `load()` gives each
    time (so a change on the settings page applies to the next transfer). `notify(title, text)`
    tells the user what arrived; it is called from the serving thread."""

    def __init__(self, load, notify=None, spawn=_spawn, startfile=_startfile, find=find_app, mark=mark_from_internet):
        self._load = load
        self._notify = notify or (lambda title, text: None)
        self._spawn = spawn
        self._startfile = startfile
        self._find = find
        self._mark = mark

    def inbox(self):
        return self._load().inbox_path()

    def decide(self, entry, offer):
        settings = self._load()
        if not settings.receive:
            return transfer.REFUSE_DECLINED
        if offer["kind"] == transfer.KIND_LAUNCH:
            allowed = allowed_app(settings, offer["app"])
            if allowed is None or not self._find(*allowed):
                LOGGER.info("Refused to start %r for %s: not an allowed app on this PC", offer["app"], entry.get("name"))
                return transfer.REFUSE_UNKNOWN_APP
            return None
        if offer["total"] > settings.max_bytes:
            return transfer.REFUSE_TOO_LARGE
        return None

    def launch(self, entry, offer):
        allowed = allowed_app(self._load(), offer["app"])
        path = self._find(*allowed) if allowed is not None else ""
        if not path:
            return False, f"{offer['app']} is not installed on this PC"
        # The sender's args are not passed on: an allowed app with arbitrary arguments is not.
        try:
            self._spawn([path])
        except OSError as exc:
            return False, f"could not start {offer['app']}: {exc}"
        self._notify(offer["app"], tr("Started for {name}").format(name=entry.get("name") or tr("the other PC")))
        return True, ""

    def received(self, entry, folder, offer):
        """Open each root of the job: folders in the editor, files of an openable kind with their
        associated app, anything else only shown. Returns (ok, error); ok when nothing failed.

        A folder opens in the editor, whose own guard (VS Code's Workspace Trust) decides whether
        anything in it may run: the inbox must never be inside a trusted folder."""
        settings = self._load()
        problems = []
        held_back = []
        self._mark(folder)
        for root in offer["roots"]:
            path = os.path.join(folder, root)
            try:
                if os.path.isdir(path):
                    editor = editor_exe(settings, self._find)
                    if editor:
                        self._spawn([editor, path])
                    else:
                        self._startfile(path)
                elif openable(path):
                    self._startfile(path)
                else:
                    held_back.append(path)
            except OSError as exc:
                problems.append(f"{root}: {exc}")
        for path in held_back:
            try:
                self._spawn([explorer_exe(), "/select,", path])
            except OSError:
                pass
        sender = plain(entry.get("name") or tr("the other PC"))
        text = plain(", ".join(offer["roots"]))
        if held_back:
            text = tr("{items} (saved and shown, not opened)").format(items=text)
        self._notify(tr("From {name}").format(name=sender), text)
        if problems:
            return False, "; ".join(problems)
        if held_back:
            return True, "saved to the inbox; only shown, since it is not a kind that is opened on arrival"
        return True, ""
