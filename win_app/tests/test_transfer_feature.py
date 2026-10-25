"""Carrying a drag across (transfer_feature.TransferFeature), with the app, the outbox, the
injector and the desktop replaced by recorders. The real QDrag needs two machines and is not run."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

import transfer_feature
from core import protocol, transfer
from core.return_edge import Rect

APP = QApplication.instance() or QApplication([])
PEER = bytes(range(1, 17))
PEER_ID = protocol.id_text(PEER)


class FakeApp(QObject):
    def __init__(self, folder):
        super().__init__()
        self.config_path = Path(folder) / "settings.json"
        self.book = mock.Mock(peers=lambda: [{"id": PEER_ID, "name": "PC2", "token": protocol.id_text(bytes(32)),
                                               "host": "10.0.0.2", "from_1_4": False, "send": True}])
        self.sender = mock.Mock()
        self.server = mock.Mock(driven=True)
        self.tray = mock.Mock()
        self.alerts = []

    def _identity(self):
        return {"id": bytes(range(101, 117))}

    def _on_alert(self, title, text):
        self.alerts.append(text)

    def _drive_status(self):
        return "status"


class FakeInjector:
    def __init__(self):
        self.calls = []

    def inject_key(self, name, down, us=None):
        self.calls.append(("key", name, down))

    def inject_mouse_button(self, button, down):
        self.calls.append(("button", button, down))


class FakeDesktop:
    def monitors(self):
        return [Rect(0, 0, 1920, 1080)]

    def cursor_position(self):
        return (1919, 270)


class FakeOutbox:
    def __init__(self):
        self.submitted, self.notices, self.launches = [], [], []
        self.files = {}

    def submit(self, paths, target_id="", carry=None):
        self.submitted.append((paths, target_id, carry))

    def send_notice(self, kind, ref, target_id):
        self.notices.append((kind, ref, target_id))

    def launch_there_or_here(self, original, app, target_id, done=None):
        self.launches.append((original, app, target_id, done))

    def sent_files(self, job):
        return self.files.get(job)

    def start(self):
        pass

    def stop(self):
        pass


class CarryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.app = FakeApp(self.folder)
        self.button = [True]
        self.injector = FakeInjector()
        self.feature = transfer_feature.TransferFeature(self.app, button_down=lambda: self.button[0],
                                                         injector=self.injector, desktop=FakeDesktop())
        self.outbox = self.feature.outbox = FakeOutbox()
        self.project = os.path.join(self.folder, "proje")
        os.makedirs(self.project)

    def test_a_dragged_folder_cancels_here_crosses_at_its_height_and_is_carried(self):
        self.feature._on_entered([self.project], PEER, "right")
        self.assertEqual(self.injector.calls[:2], [("key", "esc", True), ("key", "esc", False)])
        self.app.sender.go.assert_called_once_with(PEER, "left", 0.25)
        paths, target, job = self.outbox.submitted[0]
        self.assertEqual((paths, target), ([self.project], PEER_ID))
        self.assertTrue(job)
        self.assertTrue(self.feature._release_timer.isActive())

    def test_letting_go_here_tells_the_other_pc_once(self):
        self.feature._on_entered([self.project], PEER, "right")
        job = self.outbox.submitted[0][2]
        self.feature._poll_release()
        self.assertEqual(self.outbox.notices, [])
        self.button[0] = False
        self.feature._poll_release()
        self.feature._poll_release()
        self.assertEqual(self.outbox.notices, [(transfer.KIND_RELEASE, job, PEER_ID)])
        self.assertFalse(self.feature._release_timer.isActive())

    def test_a_dragged_shortcut_starts_there_and_crosses_only_if_it_did(self):
        shortcut = os.path.join(self.folder, "Code.lnk")
        with mock.patch.object(transfer_feature.send_win, "resolve_shortcut", return_value=r"C:\VS\Code.exe"):
            self.feature._on_entered([shortcut], PEER, "right")
        original, app, target, done = self.outbox.launches[0]
        self.assertEqual((original, app, target), (shortcut, "Code.exe", PEER_ID))
        self.app.sender.go.assert_not_called()
        done("here")
        APP.processEvents()
        self.app.sender.go.assert_not_called()
        done("there")
        APP.processEvents()
        self.app.sender.go.assert_called_once()
        self.assertEqual(self.outbox.submitted, [])

    def offer(self, job="J1"):
        return {"job": job, "kind": transfer.KIND_FILES, "roots": ["proje"], "dirs": ["proje"], "items": [], "total": 0, "carry": True}

    def test_arriving_after_the_button_was_let_go_just_opens(self):
        self.feature._on_carry_released("J1")
        with mock.patch.object(self.feature, "_open_fallback") as fallback, mock.patch.object(transfer_feature.QTimer, "singleShot"):
            self.feature._on_carry_arrived({"id": PEER_ID}, self.folder, self.offer())
        fallback.assert_called_once()
        self.assertEqual(self.injector.calls, [])

    def test_arriving_when_no_longer_driven_just_opens(self):
        self.app.server.driven = False
        with mock.patch.object(self.feature, "_open_fallback") as fallback, mock.patch.object(transfer_feature.QTimer, "singleShot"):
            self.feature._on_carry_arrived({"id": PEER_ID}, self.folder, self.offer())
        fallback.assert_called_once()
        self.assertEqual(self.injector.calls, [])

    def test_arriving_while_held_presses_the_button_and_the_release_lets_it_go(self):
        with mock.patch.object(transfer_feature.QTimer, "singleShot"):
            self.feature._on_carry_arrived({"id": PEER_ID}, self.folder, self.offer())
        self.assertEqual(self.injector.calls, [("button", "left", True)])
        self.feature._on_carry_released("J1")
        self.assertEqual(self.injector.calls[-1], ("button", "left", False))
        self.feature._let_go("J1")
        self.assertEqual(len(self.injector.calls), 2)

    def test_open_back_opens_only_what_this_pc_sent(self):
        self.assertFalse(self.feature._notice_from_server({"name": "PC2"}, {"kind": transfer.KIND_OPEN_BACK, "ref": "nope"})[0])
        self.outbox.files["J9"] = [os.path.join(self.folder, "a.psd")]
        with mock.patch.object(transfer_feature.os, "startfile", create=True) as startfile:
            ok, _ = self.feature._notice_from_server({"name": "PC2"}, {"kind": transfer.KIND_OPEN_BACK, "ref": "J9"})
        self.assertTrue(ok)
        startfile.assert_called_once_with(os.path.join(self.folder, "a.psd"))


if __name__ == "__main__":
    unittest.main()
