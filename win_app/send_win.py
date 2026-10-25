"""Sending to the other PC: what a drop on the edge or "Open on the other PC" turns into, and the
thread that sends it (core/transfer.py does the wire).

A shortcut or a program is not sent: the other PC is asked to start its own copy of the app, by
name, so it runs there on its own hardware and nothing executable crosses. A shortcut to a file or
folder sends what it points at. Everything else is sent as it is, and the other PC opens it.
"""

import base64
import logging
import os
import queue
import threading
import time

from core import protocol, transfer
from core.i18n import tr

LOGGER = logging.getLogger("Beamer")

# Paths that arrive this close together go as one job: Explorer starts one "Open on" process per
# selected item, each a cold start.
GATHER_SECONDS = 1.2


def resolve_shortcut(path):
    """A .lnk's target path, or "" when it cannot be read."""
    try:
        from win32com.client import Dispatch
    except ImportError:
        return ""
    try:
        return str(Dispatch("WScript.Shell").CreateShortcut(path).TargetPath or "")
    except Exception:
        LOGGER.info("Could not read the shortcut %s", path, exc_info=True)
        return ""


def plan(paths, resolve=None):
    """(files, launches): the paths to send as they are, and the app names to start over there."""
    resolve = resolve or resolve_shortcut
    files, launches = [], []
    for path in paths:
        if _remote(path):
            # Reading it would open an SMB connection to whoever named it, credentials and all.
            LOGGER.info("Not sending %s: only this PC's own drives are sent from", path)
            continue
        suffix = os.path.splitext(path)[1].lower()
        if suffix == ".lnk":
            target = resolve(path)
            if _remote(target):
                LOGGER.info("Not sending %s: it points at another computer", path)
            elif target.lower().endswith(".exe"):
                launches.append(os.path.basename(target))
            elif target and os.path.exists(target):
                files.append(target)
            else:
                LOGGER.info("Not sending %s: its target is not a program, file or folder here", path)
        elif suffix == ".exe":
            launches.append(os.path.basename(path))
        else:
            files.append(path)
    return _unique(files), _unique(launches)


def _remote(path):
    return str(path).replace("/", "\\").startswith("\\\\")


def _unique(items):
    seen, kept = set(), []
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            kept.append(item)
    return kept


def pick_target(peers, target_id=""):
    """The peer entry sends go to: the one `target_id` names, else the only paired one that this PC
    sends to. None when there is no such peer, or more than one and none chosen."""
    usable = [peer for peer in peers if protocol.linkable(peer) and peer.get("in_use", True) is True and peer.get("id")]
    if target_id:
        return next((peer for peer in usable if peer.get("id") == target_id), None)
    sending = [peer for peer in usable if peer.get("send", True)]
    return sending[0] if len(sending) == 1 else None


class Outbox:
    """Sends on its own thread, one job at a time, so the GUI and the hooks never wait on a disk or
    the network.

    `peers()` gives the peer entries, `settings()` the TransferSettings, `own_id()` this machine's
    16-byte id. `report(kind, text, peer_id)` is called from the sending thread with kind one of
    "progress", "done" or "failed"; `peer_id` is the target's wire id, so the app can move the
    keyboard and mouse there after a "done"."""

    def __init__(self, peers, settings, own_id, report, send=transfer.send, build=transfer.build_manifest, clock=time.monotonic,
                 open_local=None):
        self._peers = peers
        self._settings = settings
        self._own_id = own_id
        self._report = report
        self._send = send
        self._build = build
        self._clock = clock
        # What this PC opens itself when the other has nothing to open it with: the user's own
        # shortcut or file, as a double-click here would.
        self._open_local = open_local or os.startfile
        # The files of each job sent, by job id, so an open_back names only what was sent.
        self._sent = {}
        self._sent_lock = threading.Lock()
        # Each run of the thread has its own queue and stop event, so a thread still finishing a
        # send after stop() never shares either with the next one.
        self._jobs = queue.Queue()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        if self._stop.is_set():
            self._jobs, self._stop = queue.Queue(), threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._jobs, self._stop), name="Beamer-outbox", daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the thread; a send under way gives up at its next chunk."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop.set()
        self._jobs.put(None)
        thread.join(timeout=1.0)

    def submit(self, paths, target_id="", carry=None):
        """Queue `paths` for the machine `target_id` names, or the usual one (pick_target).
        `carry` is the job id of files dragged across: sent on their own, never gathered."""
        paths = [str(path) for path in paths if path]
        if paths:
            self._jobs.put((target_id or "", paths, carry))

    def sent_files(self, job):
        """The local files of job `job` sent from here, or None for a job this PC did not send."""
        with self._sent_lock:
            return list(self._sent[job]) if job in self._sent else None

    def _remember(self, job, files):
        with self._sent_lock:
            self._sent[job] = list(files)
            while len(self._sent) > 64:
                self._sent.pop(next(iter(self._sent)))

    def send_notice(self, kind, ref, target_id):
        """Send a release or open_back about job `ref` at once, on a short thread of its own: a
        release must not wait behind a large job in the queue."""
        def run():
            peer = pick_target(self._peers(), target_id)
            if peer is None:
                return
            try:
                self._send(peer, self._own_id(), notice=(kind, ref))
            except transfer.TransferError as exc:
                LOGGER.info("Could not send %s for %s: %s", kind, ref, exc)

        threading.Thread(target=run, name="Beamer-notice", daemon=True).start()

    def launch_there_or_here(self, original, app, target_id, done=None):
        """Start `app` on the other PC; when it is not installed there, open `original` (the
        shortcut or program dragged) on this one. `done(where)` gets "there", "here" or "" for a
        failure. On its own thread."""
        def run():
            done_where = ""
            peer = pick_target(self._peers(), target_id or self._settings().target)
            name = (peer or {}).get("name") or tr("the other PC")
            try:
                if peer is None:
                    raise transfer.TransferError("no peer")
                result = self._send(peer, self._own_id(), launch=(app, []))
                done_where = "there" if result["ok"] else ""
                if not result["ok"]:
                    self._report("failed", result["error"] or tr("{name} could not start {app}").format(name=name, app=app), peer["id"])
            except transfer.TransferError as exc:
                if exc.reason in (transfer.REFUSE_UNKNOWN_APP, None) or peer is None:
                    try:
                        self._open_local(original)
                        done_where = "here"
                        self._report("note", tr("{app} is not on {name}, so it opened on this PC").format(app=app, name=name), "")
                    except OSError as error:
                        self._report("failed", tr("Could not open {app}: {error}").format(app=app, error=error), "")
                else:
                    self._report("failed", _refusal_text(exc, name, app), peer["id"])
            if done is not None:
                done(done_where)

        threading.Thread(target=run, name="Beamer-launch", daemon=True).start()

    def _run(self, jobs, stop):
        waiting = []
        while not stop.is_set():
            first = waiting.pop(0) if waiting else jobs.get()
            if first is None:
                return
            target, paths, carry = first[0], list(first[1]), first[2]
            if carry is not None:
                try:
                    self.send_now(paths, target, cancelled=stop.is_set, carry=carry)
                except Exception:
                    LOGGER.exception("Sending a dragged item to the other PC failed")
                    self._report("failed", tr("Sending failed; see the log"), "")
                continue
            # Whatever else arrives for the same machine within GATHER_SECONDS goes along too.
            deadline = self._clock() + GATHER_SECONDS
            while True:
                left = deadline - self._clock()
                if left <= 0:
                    break
                try:
                    more = jobs.get(timeout=left)
                except queue.Empty:
                    break
                if more is None:
                    stop.set()
                    break
                if more[0] == target and more[2] is None:
                    paths.extend(more[1])
                else:
                    waiting.append(more)
            try:
                self.send_now(paths, target, cancelled=stop.is_set)
            except Exception:
                LOGGER.exception("Sending to the other PC failed")
                self._report("failed", tr("Sending failed; see the log"), "")

    def send_now(self, paths, target_id="", cancelled=None, carry=None):
        """Send `paths` and report how it went. Blocks; called on the outbox thread. `carry` is
        the job id of files dragged across, whose drag goes on over there."""
        settings = self._settings()
        peer = pick_target(self._peers(), target_id or settings.target)
        if peer is None:
            self._report("failed", tr("No other PC to send to: pair one first, or choose one in transfer.json"), "")
            return
        name = peer.get("name") or tr("the other PC")
        files, launches = [], []
        for path in paths:
            # One at a time, so each app keeps the shortcut it came from for a fallback here.
            more_files, apps = plan([path])
            files.extend(more_files)
            launches.extend((path, app) for app in apps)
        files = _unique(files)
        own_id = self._own_id()
        sent_any = False
        for original, app in launches:
            self._report("progress", tr("Starting {app} on {name}…").format(app=app, name=name), peer["id"])
            try:
                result = self._send(peer, own_id, launch=(app, []))
            except transfer.TransferError as exc:
                if exc.reason == transfer.REFUSE_UNKNOWN_APP:
                    # Not on the other PC: this one runs it, as a double-click here would.
                    try:
                        self._open_local(original)
                        self._report("note", tr("{app} is not on {name}, so it opened on this PC").format(app=app, name=name), "")
                    except OSError as error:
                        self._report("failed", tr("Could not open {app}: {error}").format(app=app, error=error), "")
                else:
                    self._report("failed", _refusal_text(exc, name, app), peer["id"])
                continue
            if not result["ok"]:
                self._report("failed", result["error"] or tr("{name} could not start {app}").format(name=name, app=app), peer["id"])
                continue
            sent_any = True
        if files:
            self._report("progress", tr("Preparing {count} item(s) for {name}…").format(count=len(files), name=name), peer["id"])
            try:
                manifest = self._build(files, settings.excludes)
            except (transfer.TransferError, OSError) as exc:
                self._report("failed", str(exc), peer["id"])
                return
            total = manifest.total
            last = [0.0]

            def progress(sent, whole):
                now = self._clock()
                if sent < whole and now - last[0] < 0.25:
                    return
                last[0] = now
                self._report("progress", tr("Sending to {name}: {sent} of {total}").format(name=name, sent=_size(sent), total=_size(whole)), peer["id"])

            job = carry or new_job()
            self._remember(job, files)
            try:
                result = self._send(peer, own_id, manifest=manifest, progress=progress if total else None, cancelled=cancelled,
                                    carry=carry is not None, job=job)
            except transfer.TransferError as exc:
                self._report("failed", _refusal_text(exc, name), peer["id"])
                return
            if not result["ok"]:
                self._report("failed", result["error"] or tr("{name} could not open what was sent").format(name=name), peer["id"])
                return
            sent_any = True
        if sent_any:
            # A dragged item is already in the user's hand over there: nothing to announce or follow.
            self._report("carried" if carry is not None else "done", tr("Opened on {name}").format(name=name), peer["id"])


def new_job():
    return base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")


def _refusal_text(exc, name, app=None):
    reason = getattr(exc, "reason", None)
    if reason == transfer.REFUSE_NOT_ALLOWED:
        return tr("{name} does not let this PC drive it, so it takes nothing from it either").format(name=name)
    if reason == transfer.REFUSE_UNKNOWN_APP:
        return tr("{app} is not installed on {name}, or not in its allowed apps").format(app=app, name=name)
    if reason == transfer.REFUSE_TOO_LARGE:
        return tr("Too large for {name}'s limit (transfer.json, max_bytes)").format(name=name)
    if reason == transfer.REFUSE_NO_SPACE:
        return tr("{name} does not have room for it").format(name=name)
    if reason == transfer.REFUSE_DECLINED:
        return tr("{name} is not taking transfers").format(name=name)
    return tr("Could not send to {name}: {error}").format(name=name, error=exc)


def _size(count):
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} GB"
