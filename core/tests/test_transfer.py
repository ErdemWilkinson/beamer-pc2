"""Files and launches between two paired machines (core/transfer.py), over real localhost sockets."""

import base64
import hashlib
import os
import shutil
import socket
import tempfile
import threading
import unittest

from core import protocol, receiver, transfer

SENDER_ID = bytes(range(1, 17))
RECEIVER_ID = bytes(range(101, 117))


def token():
    return protocol.id_text(os.urandom(32))


def entry(ident, tok, **extra):
    return {"id": protocol.id_text(ident), "name": "PEER", "platform": "windows", "token": tok, "host": "127.0.0.1",
            "port": 24820, "send": True, "allow_drive": True, "linked": True, "from_1_4": False, **extra}


class Rig:
    """A receiver on an ephemeral port that records what it was asked to open."""

    def __init__(self, test, tok, decide=None, **sender_entry):
        self.inbox = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.inbox, True)
        self.book = receiver.PeerBook(lambda: {"peers": [entry(SENDER_ID, tok, **sender_entry)], "zones": []}, lambda s: None)
        self.received, self.launched = [], []
        self.server = transfer.TransferServer(
            self.book, lambda: {"id": RECEIVER_ID}, lambda: self.inbox,
            decide or (lambda e, offer: None),
            lambda e, folder, offer: (self.received.append((folder, offer)) or (True, "")),
            lambda e, offer: (self.launched.append(offer) or (True, "")),
            port=0,
        )
        self.server.start()
        test.addCleanup(self.server.stop)
        self.port = self.server.listening_port


class PathTests(unittest.TestCase):
    def test_ordinary_relative_paths_pass(self):
        self.assertEqual(transfer.safe_parts("proje/src/main.py"), ("proje", "src", "main.py"))
        self.assertEqual(transfer.safe_parts("Türkçe klasör/dosya.txt"), ("Türkçe klasör", "dosya.txt"))

    def test_anything_that_could_escape_or_that_windows_refuses_is_refused(self):
        for bad in ("", "../x", "a/../../x", "a//b", "/etc/passwd", "C:/Windows/x", "C:", "a\\b", "a/./b", "x:stream",
                    "a/b.", "a/b ", "CON", "con.txt", "a/NUL", "LPT1.log", "a\x00b", "a\nb", "a" * 256, "/".join("a" * 65)):
            with self.subTest(bad=bad):
                self.assertIsNone(transfer.safe_parts(bad))

    def test_inside_never_leaves_its_folder(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertTrue(transfer._inside(folder, "a/b").startswith(os.path.abspath(folder)))
        with self.assertRaises(transfer.TransferError):
            transfer._inside(folder, "../b")


class OfferTests(unittest.TestCase):
    def offer(self, **data):
        base = {"job": "job1", "kind": "files", "roots": ["p"], "dirs": ["p"], "items": [{"path": "p/a.txt", "size": 1, "sha256": "0" * 64}]}
        base.update(data)
        return protocol.Message({"type": "offer", "data": base})

    def test_a_well_formed_offer_reads(self):
        read = transfer.read_offer(self.offer())
        self.assertEqual(read["total"], 1)
        self.assertEqual(read["roots"], ["p"])

    def test_items_outside_the_roots_or_folders_are_refused(self):
        self.assertIsNone(transfer.read_offer(self.offer(items=[{"path": "q/a.txt", "size": 1, "sha256": "0" * 64}])))
        self.assertIsNone(transfer.read_offer(self.offer(items=[{"path": "p/x/a.txt", "size": 1, "sha256": "0" * 64}])))
        self.assertIsNone(transfer.read_offer(self.offer(dirs=["p", "p/x/y"])))

    def test_a_file_cannot_be_used_as_a_folder(self):
        offer = self.offer(roots=["f"], dirs=[], items=[{"path": "f", "size": 1, "sha256": "0" * 64},
                                                        {"path": "f/inner", "size": 1, "sha256": "0" * 64}])
        self.assertIsNone(transfer.read_offer(offer))

    def test_duplicates_bad_sizes_and_bad_hashes_are_refused(self):
        twice = [{"path": "p/a.txt", "size": 1, "sha256": "0" * 64}, {"path": "p/A.TXT", "size": 1, "sha256": "0" * 64}]
        self.assertIsNone(transfer.read_offer(self.offer(items=twice)))
        self.assertIsNone(transfer.read_offer(self.offer(items=[{"path": "p/a.txt", "size": -1, "sha256": "0" * 64}])))
        self.assertIsNone(transfer.read_offer(self.offer(items=[{"path": "p/a.txt", "size": 1.0, "sha256": "0" * 64}])))
        self.assertIsNone(transfer.read_offer(self.offer(items=[{"path": "p/a.txt", "size": 1, "sha256": "zz"}])))

    def test_launch_offers_name_an_app_and_nothing_that_looks_like_a_path(self):
        read = transfer.read_offer(protocol.Message({"type": "offer", "data": {"job": "j", "kind": "launch", "app": "Code.exe"}}))
        self.assertEqual(read["app"], "Code.exe")
        for app in ("..\\x.exe", "C:/x.exe", "a/b.exe", "", "x" * 129, "evil.exe "):
            with self.subTest(app=app):
                self.assertIsNone(transfer.read_offer(protocol.Message({"type": "offer", "data": {"job": "j", "kind": "launch", "app": app}})))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, relative, data=b"x"):
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_a_folder_is_walked_and_excluded_folders_are_skipped(self):
        self.write("proje/src/main.py", b"print(1)")
        self.write("proje/node_modules/big/index.js")
        self.write("proje/.venv/lib.py")
        os.makedirs(os.path.join(self.root, "proje", "empty"))
        manifest = transfer.build_manifest([os.path.join(self.root, "proje")])
        self.assertEqual(manifest.roots, ["proje"])
        self.assertEqual([item["path"] for item in manifest.items], ["proje/src/main.py"])
        self.assertIn("proje/empty", manifest.dirs)
        self.assertNotIn("proje/node_modules", manifest.dirs)
        self.assertEqual(manifest.items[0]["sha256"], hashlib.sha256(b"print(1)").hexdigest())

    def test_two_items_of_the_same_name_are_refused(self):
        first = self.write("a/notes.txt")
        second = self.write("b/notes.txt")
        with self.assertRaises(transfer.TransferError):
            transfer.build_manifest([first, second])


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tok = token()
        self.source = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.source, True)

    def sender_entry(self, tok=None, **extra):
        return entry(RECEIVER_ID, tok or self.tok, **extra)

    def make_project(self):
        project = os.path.join(self.source, "proje")
        os.makedirs(os.path.join(project, "src"))
        with open(os.path.join(project, "src", "main.py"), "wb") as handle:
            handle.write(b"print('merhaba')\n" * 50_000)
        with open(os.path.join(project, "README.md"), "wb") as handle:
            handle.write(b"")
        return project

    def test_a_folder_arrives_whole_and_the_app_is_told_where(self):
        rig = Rig(self, self.tok)
        manifest = transfer.build_manifest([self.make_project()])
        seen = []
        result = transfer.send(self.sender_entry(), SENDER_ID, manifest=manifest, port=rig.port, progress=lambda s, t: seen.append((s, t)))
        self.assertEqual(result, {"ok": True, "error": ""})
        folder, offer = rig.received[0]
        with open(os.path.join(folder, "proje", "src", "main.py"), "rb") as handle:
            self.assertEqual(handle.read(), b"print('merhaba')\n" * 50_000)
        self.assertTrue(os.path.isfile(os.path.join(folder, "proje", "README.md")))
        self.assertEqual(seen[-1], (manifest.total, manifest.total))
        self.assertEqual([name for name in os.listdir(rig.inbox) if name.startswith(".partial")], [])

    def test_a_launch_names_only_the_app(self):
        rig = Rig(self, self.tok)
        result = transfer.send(self.sender_entry(), SENDER_ID, launch=("Code.exe", ["--new-window"]), port=rig.port)
        self.assertTrue(result["ok"])
        self.assertEqual(rig.launched[0]["app"], "Code.exe")
        self.assertEqual(rig.received, [])

    def test_a_machine_that_may_not_drive_this_one_is_refused(self):
        rig = Rig(self, self.tok, allow_drive=False)
        with self.assertRaises(transfer.TransferError) as caught:
            transfer.send(self.sender_entry(), SENDER_ID, launch=("Code.exe", []), port=rig.port)
        self.assertEqual(caught.exception.reason, transfer.REFUSE_NOT_ALLOWED)
        self.assertEqual(rig.launched, [])

    def test_the_receivers_decision_is_final(self):
        rig = Rig(self, self.tok, decide=lambda e, offer: transfer.REFUSE_UNKNOWN_APP)
        with self.assertRaises(transfer.TransferError) as caught:
            transfer.send(self.sender_entry(), SENDER_ID, launch=("evil.exe", []), port=rig.port)
        self.assertEqual(caught.exception.reason, transfer.REFUSE_UNKNOWN_APP)

    def test_another_pairs_token_gets_nowhere(self):
        rig = Rig(self, self.tok)
        with self.assertRaises(transfer.TransferError):
            transfer.send(self.sender_entry(tok=token()), SENDER_ID, launch=("Code.exe", []), port=rig.port)
        self.assertEqual(rig.launched, [])

    def test_a_hello_naming_another_machine_is_closed(self):
        rig = Rig(self, self.tok)
        with self.assertRaises(transfer.TransferError):
            transfer.send(self.sender_entry(), bytes(range(50, 66)), launch=("Code.exe", []), port=rig.port)
        self.assertEqual(rig.launched, [])

    def test_a_file_that_does_not_match_its_hash_is_thrown_away(self):
        rig = Rig(self, self.tok)
        manifest = transfer.build_manifest([self.make_project()], hasher=lambda path: "0" * 64)
        with self.assertRaises(transfer.TransferError):
            transfer.send(self.sender_entry(), SENDER_ID, manifest=manifest, port=rig.port)
        self.assertEqual(rig.received, [])
        self.assertEqual(os.listdir(rig.inbox), [])

    def test_link_frames_cannot_be_opened_on_the_transfer_channel(self):
        """The same token and handshake under the link's own label fails authentication here."""
        rig = Rig(self, self.tok)
        sock = socket.create_connection(("127.0.0.1", rig.port), timeout=2)
        self.addCleanup(sock.close)
        session = protocol.LinkSession(self.tok, protocol.ROLE_INITIATOR)
        sock.sendall(session.preamble())
        session.accept_preamble(protocol.recv_link_preamble(sock))
        protocol.send_msg(sock, session, {"type": "xfer_hello", "data": {"id": protocol.id_text(SENDER_ID)}})
        sock.settimeout(2)
        self.assertEqual(sock.recv(1), b"")
        self.assertEqual(rig.launched, [])


if __name__ == "__main__":
    unittest.main()
