"""Files, folders and app launches sent from one paired machine to another, on a port of their own.

The link (core/link.py, core/receiver.py) carries input and must stay quick, and it refuses any
frame over 64 KiB that is not a clipboard, so a project folder never travels on it. A transfer opens
its own connection to TRANSFER_PORT instead, one per job, and closes it when the job is done.
(24822 is the Mac's SSH fallback tunnel, mac_app/bridge.py, so transfers sit one above it.)

The connection is keyed exactly like a link (WIRE.md section 2: the pair's key id in a cleartext
preamble, X25519, HKDF-SHA256, ChaCha20-Poly1305 frames counted from 1) but under its own HKDF label,
so no frame of one can ever be opened as the other. After the preambles:

    sender                                   receiver
    xfer_hello {id}                 ->
                                    <-       xfer_welcome {id}
    offer {job, kind, ...}          ->
                                    <-       accept {}  |  refuse {reason}
    chunk {i, data} ... end {}      ->                         (kind "files" only)
                                    <-       result {ok, error}

The receiver admits a sender only through the paired entry its key id names, only when the
`xfer_hello` names that entry's machine, and only when that machine may drive this one
(`allow_drive`): a transfer opens things on this desktop, which is no more than driving it allows.
What arrives is checked here (every path, every size, every SHA-256) before the app is told; what
is then opened, and with what, is the app's decision (win_app/launch_win.py), never the sender's.
"""

import base64
import binascii
import hashlib
import logging
import os
import re
import shutil
import socket
import stat
import threading
import time

from core import protocol

LOGGER = logging.getLogger("Beamer")

TRANSFER_PORT = 24823
TRANSFER_LABEL = b"beamer-transfer-v1"
TRANSFER_CAP = "xfer1"

MSG_XFER_HELLO = "xfer_hello"
MSG_XFER_WELCOME = "xfer_welcome"
MSG_OFFER = "offer"
MSG_ACCEPT = "accept"
MSG_REFUSE = "refuse"
MSG_CHUNK = "chunk"
MSG_END = "end"
MSG_RESULT = "result"

KIND_FILES = "files"
KIND_LAUNCH = "launch"

CONNECT_SECONDS = 2.0
HANDSHAKE_SECONDS = 5.0
# A receiver deciding (it may ask its user) and a sender reading a slow disk both get this long.
IDLE_SECONDS = 60.0
CHUNK_BYTES = 256 * 1024
# A chunk as base64 in JSON is about 342 KiB; the offer is the only frame that may be larger.
FRAME_LIMIT = 1024 * 1024
OFFER_LIMIT = 8 * 1024 * 1024
MAX_ITEMS = 50_000
MAX_DIRS = 50_000
MAX_ROOTS = 64
MAX_PATH_CHARS = 1024
MAX_PART_CHARS = 255
MAX_DEPTH = 64
MAX_JOB_CHARS = 32
MAX_REASON_CHARS = 200
# Room left on the receiving disk beyond the job itself.
DISK_MARGIN_BYTES = 256 * 1024 * 1024
# Jobs from paired machines at once, and connections still proving they are paired: the second is
# larger and separate, so hosts that never authenticate cannot keep a paired one out.
MAX_SERVING = 4
MAX_HANDSHAKING = 16
# A job must keep up at least this pace overall, beyond a grace period, or it is dropped: a peer
# trickling one chunk a minute would otherwise hold a slot for good.
MIN_BYTES_PER_SECOND = 256 * 1024
JOB_GRACE_SECONDS = 120.0

DEFAULT_EXCLUDES = ("node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".next", "dist", "build")

REFUSE_NOT_ALLOWED = "not_allowed"
REFUSE_TOO_LARGE = "too_large"
REFUSE_NO_SPACE = "no_space"
REFUSE_DECLINED = "declined"
REFUSE_MALFORMED = "malformed"
REFUSE_UNKNOWN_APP = "unknown_app"
REFUSE_BUSY = "busy"

_JOB_SHAPE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SHA_SHAPE = re.compile(r"[0-9a-f]{64}")
_APP_SHAPE = re.compile(r"[A-Za-z0-9 ._()+-]{1,128}")
_FORBIDDEN_CHARS = set('<>:"|?*\\') | {chr(code) for code in range(32)}
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {f"{base}{n}" for base in ("COM", "LPT") for n in "123456789¹²³"}


class TransferError(Exception):
    """A transfer that did not complete. `reason` is a REFUSE_* code when the receiver refused it."""

    def __init__(self, text, reason=None):
        super().__init__(text)
        self.reason = reason


# Paths

def safe_parts(path):
    """The parts of a relative path as the wire spells it ("a/b/c.txt", forward slashes), or None
    when any part could escape the folder it is written under or is not a name Windows allows:
    empty, "." or "..", a drive or stream colon, a backslash, a control character, a trailing dot
    or space, or a reserved device name. The same rules hold on every platform, so what one machine
    sends any other can write."""
    if not isinstance(path, str) or not 1 <= len(path) <= MAX_PATH_CHARS:
        return None
    parts = path.split("/")
    if len(parts) > MAX_DEPTH:
        return None
    for part in parts:
        if not part or part in (".", "..") or len(part) > MAX_PART_CHARS:
            return None
        if any(char in _FORBIDDEN_CHARS for char in part) or part[-1] in ". ":
            return None
        if part.split(".")[0].upper().rstrip(" ") in _RESERVED_NAMES:
            return None
    return tuple(parts)


def _is_link(path):
    """A symlink or any other reparse point (a junction): never followed, so a folder that points
    elsewhere on the sending disk is not sent."""
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Manifest:
    """What a sender offers: `roots` (the top-level names, as dropped), `dirs` (every folder, so an
    empty one arrives too), `items` (each file's wire path, size and SHA-256) and, for each item,
    the local file it is read from."""

    def __init__(self):
        self.roots = []
        self.dirs = []
        self.items = []
        self.sources = []

    @property
    def total(self):
        return sum(item["size"] for item in self.items)


def build_manifest(paths, excludes=DEFAULT_EXCLUDES, hasher=file_sha256):
    """The manifest for `paths` (files and folders, each sent under its own name). Folders named in
    `excludes` are skipped wherever they are, links are never followed, and a name that could not
    be written on the other side is refused here rather than there."""
    manifest = Manifest()
    skipped = {name.lower() for name in excludes}
    for raw in paths:
        path = os.path.abspath(raw)
        name = os.path.basename(path.rstrip("\\/"))
        if safe_parts(name) is None:
            raise TransferError(f"cannot send {path!r}: its name is not one the other machine can write")
        if name.lower() in {root.lower() for root in manifest.roots}:
            raise TransferError(f"two of the items are called {name!r}")
        if not os.path.lexists(path):
            raise TransferError(f"cannot send {path!r}: it is not there")
        if _is_link(path):
            raise TransferError(f"cannot send {path!r}: it is a link")
        if os.path.isfile(path):
            manifest.roots.append(name)
            _add_file(manifest, path, name, hasher)
            continue
        if not os.path.isdir(path):
            raise TransferError(f"cannot send {path!r}: it is neither a file nor a folder")
        manifest.roots.append(name)
        manifest.dirs.append(name)
        for folder, subdirs, files in os.walk(path):
            relative = os.path.relpath(folder, path)
            prefix = name if relative == "." else name + "/" + relative.replace(os.sep, "/")
            kept = []
            for sub in sorted(subdirs):
                wire = prefix + "/" + sub
                if sub.lower() in skipped or _is_link(os.path.join(folder, sub)) or safe_parts(wire) is None:
                    continue
                kept.append(sub)
                manifest.dirs.append(wire)
            subdirs[:] = kept
            for file_name in sorted(files):
                wire = prefix + "/" + file_name
                local = os.path.join(folder, file_name)
                if _is_link(local) or safe_parts(wire) is None:
                    LOGGER.info("Not sending %s: a link or a name the other side cannot write", local)
                    continue
                _add_file(manifest, local, wire, hasher)
            if len(manifest.items) > MAX_ITEMS or len(manifest.dirs) > MAX_DIRS:
                raise TransferError(f"{name!r} holds too many files to send in one go")
    if not manifest.roots or len(manifest.roots) > MAX_ROOTS:
        raise TransferError("nothing to send" if not manifest.roots else "too many items at once")
    return manifest


def _add_file(manifest, local, wire, hasher):
    manifest.items.append({"path": wire, "size": os.path.getsize(local), "sha256": hasher(local)})
    manifest.sources.append(local)


# Messages

def _msg(message_type, /, **data):
    return {"type": message_type, "data": data}


def offer_files(job, manifest):
    return _msg(MSG_OFFER, job=job, kind=KIND_FILES, roots=list(manifest.roots), dirs=list(manifest.dirs), items=list(manifest.items))


def offer_launch(job, app, args=()):
    return _msg(MSG_OFFER, job=job, kind=KIND_LAUNCH, app=app, args=list(args))


def read_offer(message):
    """The offer's fields, checked, or None. Every path is a safe one, sits under one of the roots,
    and appears once; sizes are whole and hashes are SHA-256 hex."""
    if not isinstance(message, dict) or message.get("type") != MSG_OFFER or getattr(message, "malformed", False):
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    job, kind = data.get("job"), data.get("kind")
    if not isinstance(job, str) or not _JOB_SHAPE.fullmatch(job):
        return None
    if kind == KIND_LAUNCH:
        app, args = data.get("app"), data.get("args", [])
        if not isinstance(app, str) or not _APP_SHAPE.fullmatch(app) or app.strip(" .") != app:
            return None
        if not isinstance(args, list) or len(args) > 16 or not all(isinstance(arg, str) and len(arg) <= 1024 for arg in args):
            return None
        return {"job": job, "kind": kind, "app": app, "args": list(args)}
    if kind != KIND_FILES:
        return None
    roots, dirs, items = data.get("roots"), data.get("dirs"), data.get("items")
    if not isinstance(roots, list) or not 1 <= len(roots) <= MAX_ROOTS:
        return None
    if not isinstance(dirs, list) or len(dirs) > MAX_DIRS or not isinstance(items, list) or len(items) > MAX_ITEMS:
        return None
    root_names = set()
    for root in roots:
        parts = safe_parts(root)
        if parts is None or len(parts) != 1 or root.lower() in root_names:
            return None
        root_names.add(root.lower())
    folders = set()
    checked_dirs = []
    for path in dirs:
        parts = safe_parts(path)
        if parts is None or parts[0].lower() not in root_names or path.lower() in folders:
            return None
        if len(parts) > 1 and "/".join(parts[:-1]).lower() not in folders:
            return None
        folders.add(path.lower())
        checked_dirs.append(path)
    seen = set(folders)
    checked_items = []
    for item in items:
        if not isinstance(item, dict):
            return None
        path, size, digest = item.get("path"), item.get("size"), item.get("sha256")
        parts = safe_parts(path)
        if parts is None or parts[0].lower() not in root_names or path.lower() in seen:
            return None
        # A root that is a file is one part long; anything deeper sits in a folder that was offered.
        if len(parts) > 1 and "/".join(parts[:-1]).lower() not in folders:
            return None
        if type(size) is not int or not 0 <= size <= protocol.MAX_SAFE_INTEGER:
            return None
        if not isinstance(digest, str) or not _SHA_SHAPE.fullmatch(digest):
            return None
        seen.add(path.lower())
        checked_items.append({"path": path, "size": size, "sha256": digest})
    return {"job": job, "kind": kind, "roots": list(roots), "dirs": checked_dirs, "items": checked_items,
            "total": sum(item["size"] for item in checked_items)}


def _reason(text):
    return str(text or "")[:MAX_REASON_CHARS]


def _expect(message, kind):
    if not isinstance(message, dict) or message.get("type") != kind or not isinstance(message.get("data"), dict):
        raise TransferError(f"expected {kind}, the other machine sent {message.get('type') if isinstance(message, dict) else None!r}")
    return message["data"]


# The sending side

def send(entry, own_id, *, manifest=None, launch=None, host=None, port=TRANSFER_PORT, job=None, progress=None,
         connect=socket.create_connection, cancelled=None):
    """Send `manifest` (files and folders) or `launch` (an (app, args) pair) to the paired machine
    `entry`, and return the receiver's `result` data. Blocks; the app runs it on its own thread.
    `progress(sent, total)` is called as chunks go. Raises TransferError on any failure."""
    if (manifest is None) == (launch is None):
        raise ValueError("send takes a manifest or a launch")
    if not protocol.linkable(entry):
        raise TransferError("this machine is not paired in a way that can send files")
    host = host or entry.get("host")
    if not host:
        raise TransferError("the other machine's address is not known yet")
    job = job or base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")
    try:
        sock = connect((host, port), timeout=CONNECT_SECONDS)
    except OSError as exc:
        raise TransferError(f"could not reach {host}:{port}: {exc}") from None
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        deadline = time.monotonic() + HANDSHAKE_SECONDS
        session = protocol.LinkSession(entry["token"], protocol.ROLE_INITIATOR, label=TRANSFER_LABEL)
        sock.sendall(session.preamble())
        session.accept_preamble(protocol.recv_link_preamble(sock, deadline))
        sock.settimeout(max(0.001, deadline - time.monotonic()))
        protocol.send_msg(sock, session, _msg(MSG_XFER_HELLO, id=protocol.peer_id_text(own_id)))
        welcome = _expect(protocol.recv_first_msg(sock, session, deadline), MSG_XFER_WELCOME)
        if welcome.get("id") != entry.get("id"):
            raise TransferError("a different machine answered at that address")
        sock.settimeout(IDLE_SECONDS)
        offer = offer_files(job, manifest) if manifest is not None else offer_launch(job, *launch)
        protocol.send_msg(sock, session, offer)
        answer = protocol.recv_msg(sock, session, FRAME_LIMIT)
        if isinstance(answer, dict) and answer.get("type") == MSG_REFUSE:
            data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
            reason = _reason(data.get("reason") if isinstance(data.get("reason"), str) else REFUSE_DECLINED)
            raise TransferError(f"the other machine refused: {reason}", reason=reason)
        _expect(answer, MSG_ACCEPT)
        if manifest is not None:
            _send_chunks(sock, session, manifest, progress, cancelled)
            protocol.send_msg(sock, session, _msg(MSG_END))
        result = _expect(protocol.recv_msg(sock, session, FRAME_LIMIT), MSG_RESULT)
        return {"ok": result.get("ok") is True, "error": _reason(result.get("error")) if isinstance(result.get("error"), str) else ""}
    except TransferError:
        raise
    except protocol.AuthenticationError:
        raise TransferError("the other machine could not be authenticated") from None
    except (protocol.ProtocolError, protocol.ConnectionClosed, OSError) as exc:
        raise TransferError(f"the transfer failed: {exc}") from None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _send_chunks(sock, session, manifest, progress, cancelled):
    total, sent = manifest.total, 0
    for index, (item, source) in enumerate(zip(manifest.items, manifest.sources)):
        remaining = item["size"]
        with open(source, "rb") as handle:
            while remaining > 0:
                if cancelled is not None and cancelled():
                    raise TransferError("cancelled")
                block = handle.read(min(CHUNK_BYTES, remaining))
                if not block:
                    raise TransferError(f"{source} got shorter while it was being sent")
                protocol.send_msg(sock, session, _msg(MSG_CHUNK, i=index, data=base64.b64encode(block).decode("ascii")))
                remaining -= len(block)
                sent += len(block)
                if progress is not None:
                    progress(sent, total)


# The receiving side

class TransferServer:
    """Listens on `port` and serves each sender on its own thread, at most MAX_SERVING at once.

    `book` is the link's PeerBook. `identity()` gives this machine's id as 16 bytes under "id".
    `inbox()` is the folder received jobs are written under. `decide(entry, offer)` returns None to
    accept or a REFUSE_* reason; `received(entry, folder, offer)` and `launch(entry, offer)` do the
    opening and return (ok, error). The callbacks run on the serving thread."""

    def __init__(self, book, identity, inbox, decide, received, launch, port=TRANSFER_PORT):
        self._book = book
        self._identity = identity
        self._inbox = inbox
        self._decide = decide
        self._received = received
        self._launch = launch
        self.port = port
        self._lock = threading.Lock()
        self._serving = 0
        self._handshaking = 0
        # Bytes promised to jobs still arriving, so jobs at once cannot each count the same free space.
        self._reserved = 0
        self._stop = threading.Event()
        self._listener = None
        self._thread = None

    @property
    def listening_port(self):
        with self._lock:
            return self._listener.getsockname()[1] if self._listener is not None else None

    def start(self):
        """Bind and serve. Raises OSError when the port is taken; the app reports it and goes on."""
        if self._thread is not None:
            return
        self._stop.clear()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("0.0.0.0", self.port))
            listener.listen(16)
        except OSError:
            listener.close()
            raise
        listener.settimeout(0.2)
        with self._lock:
            self._listener = listener
        self._sweep()
        self._thread = threading.Thread(target=self._accept_loop, args=(listener,), name="Beamer-transfer", daemon=True)
        self._thread.start()
        LOGGER.info("Listening for transfers on 0.0.0.0:%s", listener.getsockname()[1])

    def stop(self):
        self._stop.set()
        with self._lock:
            listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _sweep(self):
        """Staging folders a crash left behind: never opened, so only in the way."""
        try:
            inbox = self._inbox()
            names = os.listdir(inbox)
        except OSError:
            return
        for name in names:
            if name.startswith(".partial-"):
                shutil.rmtree(os.path.join(inbox, name), ignore_errors=True)

    def _accept_loop(self, listener):
        while not self._stop.is_set():
            try:
                connection, address = listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set() or listener.fileno() == -1:
                    return
                # An aborted connection or a moment out of handles is not the end of listening.
                LOGGER.warning("Accepting a transfer failed: %s", exc)
                self._stop.wait(0.5)
                continue
            with self._lock:
                full = self._handshaking >= MAX_HANDSHAKING
                if not full:
                    self._handshaking += 1
            if full:
                connection.close()
                continue
            threading.Thread(target=self._serve, args=(connection, address[0]), name="Beamer-transfer-job", daemon=True).start()

    def _serve(self, connection, host):
        held = {"handshaking": True, "serving": False}
        try:
            self._serve_one(connection, host, held)
        except (protocol.ProtocolError, protocol.ConnectionClosed, OSError, TransferError) as exc:
            LOGGER.info("A transfer from %s ended: %s", host, exc)
        except Exception:
            LOGGER.exception("A transfer from %s failed", host)
        finally:
            with self._lock:
                if held["handshaking"]:
                    self._handshaking -= 1
                if held["serving"]:
                    self._serving -= 1
            try:
                connection.close()
            except OSError:
                pass

    def _admitted(self, held):
        """Move a connection that proved its pairing from the handshake count to a job slot; False
        when every slot is taken."""
        with self._lock:
            self._handshaking -= 1
            held["handshaking"] = False
            if self._serving >= MAX_SERVING:
                return False
            self._serving += 1
            held["serving"] = True
            return True

    def _serve_one(self, connection, host, held):
        deadline = time.monotonic() + HANDSHAKE_SECONDS
        preamble = protocol.recv_link_preamble(connection, deadline)
        _, key_id, _ = protocol.split_preamble(preamble)
        entry = self._book.find(key_id)
        if entry is None:
            LOGGER.info("%s offered a transfer under a pairing this machine does not have; closed", host)
            return
        session = protocol.LinkSession(entry["token"], protocol.ROLE_RESPONDER, label=TRANSFER_LABEL)
        session.accept_preamble(preamble)
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.sendall(session.preamble())
        hello = _expect(protocol.recv_first_msg(connection, session, deadline), MSG_XFER_HELLO)
        own_id = protocol.id_text(bytes(self._identity()["id"]))
        if hello.get("id") != entry.get("id") or hello.get("id") == own_id:
            LOGGER.warning("%s sent a transfer hello naming another machine; closed", host)
            return
        protocol.send_msg(connection, session, _msg(MSG_XFER_WELCOME, id=own_id))
        connection.settimeout(IDLE_SECONDS)
        # The offer is read even when it will be refused, so the refusal reaches a sender that is
        # still writing it rather than a reset connection.
        admitted = self._admitted(held)
        offer = read_offer(protocol.recv_msg(connection, session, OFFER_LIMIT))
        if not admitted:
            protocol.send_msg(connection, session, _msg(MSG_REFUSE, reason=REFUSE_BUSY))
            return
        if entry.get("in_use", True) is not True or entry.get("allow_drive") is not True:
            protocol.send_msg(connection, session, _msg(MSG_REFUSE, reason=REFUSE_NOT_ALLOWED))
            LOGGER.info("Refused a transfer from %s: it may not drive this machine", entry.get("name") or host)
            return
        if offer is None:
            protocol.send_msg(connection, session, _msg(MSG_REFUSE, reason=REFUSE_MALFORMED))
            return
        refusal = self._decide(entry, offer)
        reserved = 0
        if refusal is None and offer["kind"] == KIND_FILES:
            refusal = self._reserve(offer)
            reserved = offer["total"] if refusal is None else 0
        try:
            if refusal is not None:
                protocol.send_msg(connection, session, _msg(MSG_REFUSE, reason=_reason(refusal)))
                return
            protocol.send_msg(connection, session, _msg(MSG_ACCEPT))
            if offer["kind"] == KIND_LAUNCH:
                ok, error = self._launch(entry, offer)
            else:
                folder = self._receive_files(connection, session, offer)
                ok, error = self._received(entry, folder, offer)
        finally:
            with self._lock:
                self._reserved -= reserved
        protocol.send_msg(connection, session, _msg(MSG_RESULT, ok=bool(ok), error=_reason(error) if error else ""))

    def _reserve(self, offer):
        """None, with the job's bytes held against the inbox's free space, or REFUSE_NO_SPACE."""
        try:
            inbox = self._inbox()
            os.makedirs(inbox, exist_ok=True)
            free = shutil.disk_usage(inbox).free
        except OSError:
            LOGGER.exception("Could not prepare the transfer inbox")
            return REFUSE_NO_SPACE
        with self._lock:
            if self._reserved + offer["total"] + DISK_MARGIN_BYTES > free:
                return REFUSE_NO_SPACE
            self._reserved += offer["total"]
        return None

    def _receive_files(self, connection, session, offer):
        """Write the offered files under a new folder in the inbox, checking each one's size and
        SHA-256, and return that folder. Written to a hidden staging folder first and renamed only
        when every file checks out, so a half-received job is never opened."""
        inbox = os.path.abspath(self._inbox())
        staging = os.path.join(inbox, f".partial-{offer['job']}")
        final = os.path.join(inbox, f"{time.strftime('%Y-%m-%d_%H-%M-%S')}_{os.urandom(4).hex()}")
        deadline = time.monotonic() + JOB_GRACE_SECONDS + offer["total"] / MIN_BYTES_PER_SECOND
        os.makedirs(staging, exist_ok=False)
        try:
            for path in offer["dirs"]:
                os.makedirs(_inside(staging, path), exist_ok=True)
            for index, item in enumerate(offer["items"]):
                target = _inside(staging, item["path"])
                os.makedirs(os.path.dirname(target), exist_ok=True)
                digest = hashlib.sha256()
                written = 0
                with open(target, "xb") as handle:
                    while written < item["size"]:
                        if time.monotonic() > deadline:
                            raise TransferError("the job arrived too slowly")
                        data = _expect(protocol.recv_msg(connection, session, FRAME_LIMIT), MSG_CHUNK)
                        if data.get("i") != index or not isinstance(data.get("data"), str):
                            raise TransferError("a chunk arrived out of order")
                        try:
                            block = base64.b64decode(data["data"], validate=True)
                        except (binascii.Error, ValueError):
                            raise TransferError("a chunk was not base64") from None
                        if not block or written + len(block) > item["size"]:
                            raise TransferError(f"{item['path']} is larger than offered")
                        handle.write(block)
                        digest.update(block)
                        written += len(block)
                if digest.hexdigest() != item["sha256"]:
                    raise TransferError(f"{item['path']} did not arrive intact")
            _expect(protocol.recv_msg(connection, session, FRAME_LIMIT), MSG_END)
            os.replace(staging, final)
            return final
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise


def _inside(folder, wire_path):
    """`wire_path` (already safe_parts-checked) under `folder`, refusing anything that would land
    outside it all the same."""
    parts = safe_parts(wire_path)
    if parts is None:
        raise TransferError(f"refused the path {wire_path!r}")
    root = os.path.abspath(folder)
    target = os.path.abspath(os.path.join(root, *parts))
    if os.path.commonpath([root, target]) != root or target == root:
        raise TransferError(f"refused the path {wire_path!r}")
    return target
