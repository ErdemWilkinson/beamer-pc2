"""What this PC opens when a transfer arrives (launch_win), with every spawn and startfile recorded
rather than run."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import launch_win
from core import transfer

PEER = {"id": "AAAAAAAAAAAAAAAAAAAAAA", "name": "MAIN"}
CODE = r"C:\Programs\Code.exe"


class Rig:
    def __init__(self, test, settings=None, installed=(("Code.exe", CODE),), associated=lambda path: True, ask_back=True):
        self.settings = settings or launch_win.TransferSettings()
        self.spawned, self.started, self.notes, self.marked, self.carried, self.asked = [], [], [], [], [], []
        installed = {name.lower(): path for name, path in installed}
        self.receiver = launch_win.Receiver(
            lambda: self.settings,
            notify=lambda title, text: self.notes.append((title, text)),
            spawn=self.spawned.append,
            startfile=self.started.append,
            find=lambda name, configured="": installed.get(name.lower(), ""),
            mark=self.marked.append,
            carry=lambda entry, folder, offer: (self.carried.append(offer["job"]) or (True, "")),
            notice=lambda entry, offer: (True, ""),
            ask_back=(lambda entry, offer: (self.asked.append(offer["job"]) or (True, ""))) if ask_back else None,
            associated=associated,
        )
        self.folder = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.folder, True)

    def make(self, *names, folder=False):
        for name in names:
            path = os.path.join(self.folder, name)
            if folder:
                os.makedirs(path)
            else:
                with open(path, "wb") as handle:
                    handle.write(b"x")


def files_offer(*roots, total=1):
    return {"job": "j", "kind": transfer.KIND_FILES, "roots": list(roots), "dirs": [], "items": [], "total": total}


def launch_offer(app):
    return {"job": "j", "kind": transfer.KIND_LAUNCH, "app": app, "args": ["--evil"]}


class DecideTests(unittest.TestCase):
    def test_an_allowed_installed_app_may_start(self):
        rig = Rig(self)
        self.assertIsNone(rig.receiver.decide(PEER, launch_offer("code.EXE")))

    def test_an_app_not_on_the_list_or_not_installed_is_refused(self):
        rig = Rig(self)
        self.assertEqual(rig.receiver.decide(PEER, launch_offer("cmd.exe")), transfer.REFUSE_UNKNOWN_APP)
        self.assertEqual(rig.receiver.decide(PEER, launch_offer("Antigravity.exe")), transfer.REFUSE_UNKNOWN_APP)

    def test_receiving_off_refuses_everything(self):
        rig = Rig(self, launch_win.TransferSettings(receive=False))
        self.assertEqual(rig.receiver.decide(PEER, files_offer("a.txt")), transfer.REFUSE_DECLINED)

    def test_too_large_is_refused(self):
        rig = Rig(self, launch_win.TransferSettings(max_bytes=10))
        self.assertEqual(rig.receiver.decide(PEER, files_offer("a.txt", total=11)), transfer.REFUSE_TOO_LARGE)


class OpenTests(unittest.TestCase):
    def test_a_launch_starts_the_local_copy_without_the_senders_arguments(self):
        rig = Rig(self)
        self.assertEqual(rig.receiver.launch(PEER, launch_offer("Code.exe")), (True, ""))
        self.assertEqual(rig.spawned, [[CODE]])

    def test_a_folder_opens_in_the_editor(self):
        rig = Rig(self)
        rig.make("proje", folder=True)
        ok, _ = rig.receiver.received(PEER, rig.folder, files_offer("proje"))
        self.assertTrue(ok)
        self.assertEqual(rig.spawned, [[CODE, os.path.join(rig.folder, "proje")]])

    def test_a_folder_opens_in_explorer_when_the_editor_is_missing(self):
        rig = Rig(self, installed=())
        rig.make("proje", folder=True)
        rig.receiver.received(PEER, rig.folder, files_offer("proje"))
        self.assertEqual(rig.started, [os.path.join(rig.folder, "proje")])

    def test_a_document_opens_with_its_app(self):
        rig = Rig(self)
        rig.make("notlar.txt")
        rig.receiver.received(PEER, rig.folder, files_offer("notlar.txt"))
        self.assertEqual(rig.started, [os.path.join(rig.folder, "notlar.txt")])

    def test_anything_not_known_to_be_harmless_is_only_shown(self):
        names = ("setup.exe", "run.BAT", "x.ps1", "y.lnk", "rapor.docm", "sayfa.html", "tema.theme", "uzak.rdp", "garip.xyz")
        rig = Rig(self, ask_back=False)
        rig.make(*names)
        ok, error = rig.receiver.received(PEER, rig.folder, files_offer(*names))
        self.assertTrue(ok)
        self.assertIn("only shown", error)
        self.assertEqual(rig.started, [])
        self.assertTrue(all(argv[0].lower().endswith("explorer.exe") and os.path.isabs(argv[0]) and argv[1] == "/select,"
                            for argv in rig.spawned))
        self.assertEqual(len(rig.spawned), len(names))

    def test_a_carried_job_goes_to_the_drag_unopened_but_marked(self):
        rig = Rig(self)
        rig.make("notlar.txt")
        offer = dict(files_offer("notlar.txt"), carry=True)
        rig.receiver.received(PEER, rig.folder, offer)
        self.assertEqual(rig.carried, ["j"])
        self.assertEqual(rig.started, [])
        self.assertEqual(rig.marked, [rig.folder])

    def test_files_nothing_here_opens_are_opened_by_the_sender(self):
        rig = Rig(self, associated=lambda path: False)
        rig.make("tasarim.psd", "notlar.txt")
        ok, error = rig.receiver.received(PEER, rig.folder, files_offer("tasarim.psd", "notlar.txt"))
        self.assertTrue(ok)
        self.assertIn("sending PC", error)
        self.assertEqual(rig.asked, ["j"])
        self.assertEqual(rig.started, [])

    def test_a_folder_or_one_openable_file_keeps_the_job_here(self):
        rig = Rig(self, associated=lambda path: path.endswith(".txt"))
        rig.make("tasarim.psd", "notlar.txt")
        rig.receiver.received(PEER, rig.folder, files_offer("tasarim.psd", "notlar.txt"))
        self.assertEqual(rig.asked, [])
        self.assertEqual(rig.started, [os.path.join(rig.folder, "notlar.txt")])

    def test_notices_pass_the_decision_and_reach_the_handler(self):
        rig = Rig(self)
        self.assertIsNone(rig.receiver.decide(PEER, {"job": "n", "kind": transfer.KIND_RELEASE, "ref": "j"}))
        bare = launch_win.Receiver(lambda: launch_win.TransferSettings())
        self.assertEqual(bare.decide(PEER, {"job": "n", "kind": transfer.KIND_RELEASE, "ref": "j"}), transfer.REFUSE_DECLINED)

    def test_what_arrives_is_marked_as_from_the_internet_first(self):
        rig = Rig(self)
        rig.make("notlar.txt")
        rig.receiver.received(PEER, rig.folder, files_offer("notlar.txt"))
        self.assertEqual(rig.marked, [rig.folder])

    def test_notices_drop_bidirectional_overrides(self):
        rig = Rig(self)
        name = "resim‮gnp.exe"
        rig.make(name)
        rig.receiver.received(PEER, rig.folder, files_offer(name))
        self.assertNotIn("‮", rig.notes[-1][1])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.config = os.path.join(self.folder, "settings.json")

    def test_missing_or_broken_files_give_the_defaults(self):
        self.assertEqual(launch_win.load_settings(self.config), launch_win.TransferSettings())
        with open(launch_win.settings_path(self.config), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(launch_win.load_settings(self.config), launch_win.TransferSettings())

    def test_wrong_types_fall_back_field_by_field(self):
        with open(launch_win.settings_path(self.config), "w", encoding="utf-8") as handle:
            json.dump({"receive": "yes", "editor": "antigravity", "max_bytes": 5, "apps": {"Code.exe": 3, "x.exe": ""}}, handle)
        loaded = launch_win.load_settings(self.config)
        self.assertTrue(loaded.receive)
        self.assertEqual(loaded.editor, "antigravity")
        self.assertEqual(loaded.max_bytes, 5)
        self.assertEqual(loaded.apps, {"x.exe": ""})

    def test_settings_round_trip(self):
        settings = launch_win.TransferSettings(editor="custom", editor_path=r"C:\x.exe", follow=False)
        launch_win.save_settings(self.config, settings)
        self.assertEqual(launch_win.load_settings(self.config), settings)


class DeelevatedTests(unittest.TestCase):
    """With administrator rights (pc2-hizlandir.ps1), nothing a peer starts may inherit them."""

    def test_a_normal_beamer_starts_things_as_they_are(self):
        self.assertEqual(["C:/Code/Code.exe", "C:/In box/proj"],
                         launch_win.deelevated(["C:/Code/Code.exe", "C:/In box/proj"], elevated=False))

    def test_an_elevated_beamer_goes_through_runas_basic_user(self):
        argv = launch_win.deelevated([r"C:\Windows\explorer.exe", "/select,", r"C:\In box\a,b.txt"], elevated=True)
        self.assertTrue(argv[0].lower().endswith(r"system32\runas.exe"))
        self.assertEqual("/trustlevel:0x20000", argv[1])
        # Paths quoted, so a space or Explorer's comma cannot split them; the switch left bare.
        self.assertEqual(r'"C:\Windows\explorer.exe" /select, "C:\In box\a,b.txt"', argv[2])
        self.assertEqual(3, len(argv))


if __name__ == "__main__":
    unittest.main()
