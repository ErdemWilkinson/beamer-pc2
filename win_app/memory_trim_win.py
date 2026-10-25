"""Give idle memory back to Windows while Beamer sits in the tray.

Most of what Beamer holds is Qt and the settings window, built at start and seldom shown. Every few
minutes, while the window is hidden and input is on this PC, the garbage collector runs and the
working set is emptied: Windows takes the pages back and faults in only those used again, so the
figure Task Manager shows drops from about 170 MB to about 30 MB. Nothing is trimmed while another
machine is driven from here or drives this one, so input never waits on a page coming back.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import sys

from PySide6.QtCore import QObject, QTimer

LOGGER = logging.getLogger("Beamer")
FIRST_MS = 20_000
EVERY_MS = 180_000


def trim() -> bool:
    """Collect garbage and empty this process's working set; False where that is not possible."""
    gc.collect()
    if sys.platform != "win32":
        return False
    try:
        kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        return bool(psapi.EmptyWorkingSet(kernel32.GetCurrentProcess()))
    except (AttributeError, OSError):
        LOGGER.debug("Could not trim the working set", exc_info=True)
        return False


class Trimmer(QObject):
    """Trims on a timer whenever `idle()` says nothing would notice. `trim` is injected by tests."""

    def __init__(self, parent, idle, trim=trim):
        super().__init__(parent)
        self._idle = idle
        self._trim = trim
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        QTimer.singleShot(FIRST_MS, self._tick)
        self._timer.start(EVERY_MS)

    def _tick(self):
        try:
            if self._idle():
                self._trim()
        except Exception:
            LOGGER.exception("Memory trim failed")

    def stop(self):
        self._timer.stop()
