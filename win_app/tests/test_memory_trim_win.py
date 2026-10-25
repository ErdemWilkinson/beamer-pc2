"""Giving idle memory back (memory_trim_win), with the trim itself replaced by a counter."""

import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PySide6.QtWidgets import QApplication

import memory_trim_win

APP = QApplication.instance() or QApplication([])


class TrimmerTest(unittest.TestCase):
    def make(self, idle):
        trims = []
        trimmer = memory_trim_win.Trimmer(None, lambda: idle[0], trim=lambda: trims.append(1))
        self.addCleanup(trimmer.stop)
        return trimmer, trims

    def test_trims_only_while_nothing_would_notice(self):
        idle = [False]
        trimmer, trims = self.make(idle)
        trimmer._tick()
        self.assertEqual([], trims)
        idle[0] = True
        trimmer._tick()
        self.assertEqual([1], trims)

    def test_a_failing_check_never_stops_the_app(self):
        def broken():
            raise RuntimeError("boom")

        trimmer = memory_trim_win.Trimmer(None, broken, trim=lambda: None)
        self.addCleanup(trimmer.stop)
        with self.assertLogs("Beamer", "ERROR"):
            trimmer._tick()

    @unittest.skipUnless(sys.platform == "win32", "EmptyWorkingSet is Windows")
    def test_the_real_trim_succeeds_on_windows(self):
        self.assertTrue(memory_trim_win.trim())


if __name__ == "__main__":
    unittest.main()
