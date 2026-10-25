"""Updates for a Beamer PC2 checkout (fork_update_win), with git and the process launcher replaced."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import fork_update_win
from core.updates import Release


class FakeGit:
    """Answers the commands GitChecker runs from a table; records each one."""

    def __init__(self, ahead=0, behind=0, dirty="", head="1a2b3c4", fail=None):
        self.answers = {
            "fetch": "",
            "rev-list": f"{ahead}\t{behind}",
            "status": dirty,
            "rev-parse": head,
        }
        self.fail = fail
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == self.fail:
            raise OSError(f"git {args[0]} failed")
        return self.answers[args[0]]


def checker(git, install=False):
    results, checks = [], []
    found = fork_update_win.GitChecker(
        "1.5.13", lambda: True, results.append, "C:/nowhere", git=git, install=install,
        on_check=lambda state, release: checks.append((state, release)))
    return found, results, checks


class OfferedTest(unittest.TestCase):
    def test_nothing_new_is_never_offered(self):
        self.assertFalse(fork_update_win.offered(0, 0, True, True))

    def test_an_install_is_always_brought_in_line(self):
        # A rewritten history leaves an install both ahead and behind; it holds nothing of its own.
        self.assertTrue(fork_update_win.offered(3, 2, False, True))

    def test_a_development_copy_is_offered_only_what_a_reset_cannot_lose(self):
        self.assertTrue(fork_update_win.offered(2, 0, True, False))
        self.assertFalse(fork_update_win.offered(2, 1, True, False))
        self.assertFalse(fork_update_win.offered(2, 0, False, False))


class GitCheckerTest(unittest.TestCase):
    def test_commits_on_github_are_an_update(self):
        git = FakeGit(behind=2)
        found, results, _checks = checker(git)
        found.check_once()
        self.assertEqual([Release("PC2 1a2b3c4", fork_update_win.REPO_PAGE, True)], results)
        self.assertEqual(("fetch", "--quiet", "origin", "main"), git.calls[0])

    def test_an_up_to_date_checkout_is_current(self):
        found, _results, checks = checker(FakeGit())
        found.check_once(manual=True)
        self.assertEqual([("checking", None), ("current", None)], checks)

    def test_a_failed_fetch_is_a_failed_check(self):
        found, results, checks = checker(FakeGit(behind=2, fail="fetch"))
        found.check_once(manual=True)
        self.assertEqual([("checking", None), ("failed", None)], checks)
        self.assertEqual([], results)

    def test_a_development_copy_with_its_own_changes_is_left_alone(self):
        found, results, _checks = checker(FakeGit(behind=2, dirty=" M core/updates.py"))
        found.check_once()
        self.assertEqual([None], results)

    def test_an_install_is_checked_although_its_version_file_never_changes(self):
        found, results, _checks = checker(FakeGit(behind=1, ahead=4), install=True)
        found.check_once()
        self.assertEqual("PC2 1a2b3c4", results[0][0])


class PlacesTest(unittest.TestCase):
    def test_the_install_folder_is_the_one_kur_ps1_uses(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertTrue(fork_update_win.is_install(Path(home, "beamer-pc2"), home=home))
            self.assertFalse(fork_update_win.is_install(Path(home, "elsewhere"), home=home))

    def test_only_a_checkout_of_the_fork_updates_itself(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(fork_update_win.fork_checkout(root))
            Path(root, ".git").mkdir()
            config = Path(root, ".git", "config")
            config.write_text('[remote "origin"]\n\turl = https://github.com/kalkman-code/beamer.git\n')
            self.assertFalse(fork_update_win.fork_checkout(root))
            config.write_text('[remote "origin"]\n\turl = https://github.com/ErdemWilkinson/beamer-pc2.git\n')
            self.assertTrue(fork_update_win.fork_checkout(root))

    def test_only_the_install_updates_itself_never_a_working_copy(self):
        # A working copy that updated itself would be reset to GitHub, uncommitted work and all.
        with tempfile.TemporaryDirectory() as home:
            for name in ("beamer-pc2", "work"):
                root = Path(home, name)
                Path(root, ".git").mkdir(parents=True)
                Path(root, ".git", "config").write_text("url = https://github.com/ErdemWilkinson/beamer-pc2.git\n")
                Path(root, "kur.ps1").write_text("")
            self.assertEqual(sys.platform == "win32", fork_update_win.available(Path(home, "beamer-pc2"), home=home))
            self.assertFalse(fork_update_win.available(Path(home, "work"), home=home))


class StartUpdateTest(unittest.TestCase):
    def test_kur_ps1_runs_in_update_mode_for_this_checkout(self):
        seen = {}

        def popen(command, **kwargs):
            seen.update(kwargs, command=command)

        fork_update_win.start_update("C:/Users/me/beamer-pc2", popen=popen)
        self.assertEqual(str(Path("C:/Users/me/beamer-pc2", "kur.ps1")), seen["command"][-1])
        self.assertIn("-File", seen["command"])
        self.assertEqual("1", seen["env"]["BEAMER_PC2_UPDATE"])
        self.assertEqual(str(Path("C:/Users/me/beamer-pc2")), seen["env"]["BEAMER_PC2_DIR"])


if __name__ == "__main__":
    unittest.main()
