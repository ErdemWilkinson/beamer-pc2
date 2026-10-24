"""Turning dropped paths into transfers, and the outbox that sends them (send_win), with the wire
replaced by a recorder."""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import launch_win
import send_win
from core import protocol, transfer


def peer(name="PC2", **extra):
    return {"id": protocol.id_text(os.urandom(16)), "name": name, "token": protocol.id_text(os.urandom(32)),
            "host": "10.0.0.2", "send": True, "from_1_4": False, **extra}


class PlanTests(unittest.TestCase):
    def test_programs_become_launches_and_everything_else_is_sent(self):
        targets = {r"C:\Desk\VS Code.lnk": r"C:\Programs\Microsoft VS Code\Code.exe"}
        files, launches = send_win.plan([r"C:\Desk\VS Code.lnk", r"C:\Tools\Antigravity.exe", r"C:\Work\proje", r"C:\a.txt"],
                                        resolve=targets.get)
        self.assertEqual(launches, ["Code.exe", "Antigravity.exe"])
        self.assertEqual(files, [r"C:\Work\proje", r"C:\a.txt"])

    def test_a_shortcut_to_a_folder_sends_the_folder(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        files, launches = send_win.plan([r"C:\Desk\Proje.lnk"], resolve=lambda path: folder)
        self.assertEqual((files, launches), ([folder], []))

    def test_a_broken_shortcut_sends_nothing(self):
        self.assertEqual(send_win.plan([r"C:\Desk\gone.lnk"], resolve=lambda path: ""), ([], []))


class TargetTests(unittest.TestCase):
    def test_the_only_paired_machine_is_the_target(self):
        only = peer()
        self.assertIs(send_win.pick_target([only]), only)

    def test_two_machines_need_a_choice(self):
        first, second = peer("A"), peer("B")
        self.assertIsNone(send_win.pick_target([first, second]))
        self.assertIs(send_win.pick_target([first, second], second["id"]), second)

    def test_unpaired_or_unused_machines_never_are(self):
        self.assertIsNone(send_win.pick_target([peer(token="typed"), peer(in_use=False)]))


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.peer = peer()
        self.sent, self.reports = [], []
        self.settings = launch_win.TransferSettings()

    def outbox(self, send=None):
        def record(entry, own_id, **kwargs):
            self.sent.append(kwargs)
            return {"ok": True, "error": ""}

        return send_win.Outbox(lambda: [self.peer], lambda: self.settings, lambda: b"\x01" * 16,
                               lambda kind, text, peer_id: self.reports.append((kind, text, peer_id)),
                               send=send or record)

    def test_a_folder_is_sent_and_reported_done_for_its_peer(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        with open(os.path.join(folder, "a.txt"), "wb") as handle:
            handle.write(b"hi")
        self.outbox().send_now([folder])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["manifest"].roots, [os.path.basename(folder)])
        self.assertEqual(self.reports[-1], ("done", "Opened on PC2", self.peer["id"]))

    def test_a_refusal_is_reported_in_words(self):
        def refuse(entry, own_id, **kwargs):
            raise transfer.TransferError("refused", reason=transfer.REFUSE_UNKNOWN_APP)

        self.outbox(refuse).send_now([r"C:\Tools\Antigravity.exe"])
        self.assertEqual(self.reports[-1][0], "failed")
        self.assertIn("Antigravity.exe is not installed on PC2", self.reports[-1][1])

    def test_without_a_target_nothing_is_sent(self):
        self.peer = peer(in_use=False)
        self.outbox().send_now([r"C:\a.txt"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.reports[-1][0], "failed")

    def run_outbox(self, *submissions, count=1):
        jobs = []
        box = self.outbox()
        box.send_now = lambda paths, target, cancelled=None: jobs.append((target, paths))
        for submission in submissions:
            box.submit(*submission)
        box.start()
        self.addCleanup(box.stop)
        for _ in range(250):
            if len(jobs) >= count:
                break
            time.sleep(0.02)
        return jobs

    def test_paths_arriving_together_are_one_job(self):
        self.assertEqual(self.run_outbox((["a"],), (["b"],)), [("", ["a", "b"])])

    def test_jobs_for_different_machines_stay_apart(self):
        jobs = self.run_outbox((["a"], "PC2"), (["b"], "PC3"), (["c"], "PC2"), count=2)
        self.assertEqual(jobs, [("PC2", ["a", "c"]), ("PC3", ["b"])])

    def test_the_outbox_works_again_after_a_stop(self):
        box = self.outbox()
        box.stop()
        box.start()
        box.stop()
        jobs = []
        box.send_now = lambda paths, target, cancelled=None: jobs.append(paths)
        box.start()
        self.addCleanup(box.stop)
        box.submit(["a"])
        for _ in range(250):
            if jobs:
                break
            time.sleep(0.02)
        self.assertEqual(jobs, [["a"]])

    def test_network_paths_are_never_read(self):
        self.assertEqual(send_win.plan([r"\\attacker\share\x.txt", "//host/s/y"]), ([], []))
        self.assertEqual(send_win.plan([r"C:\Desk\x.lnk"], resolve=lambda path: r"\\host\s\app.exe"), ([], []))


if __name__ == "__main__":
    unittest.main()
