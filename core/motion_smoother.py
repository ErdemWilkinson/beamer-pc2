"""Pointer motion from a peer, spread over a short glide instead of applied the moment it arrives.

Over a jittery Wi-Fi link the moves of a steadily moving mouse arrive in bursts: nothing for
20-60 ms, then everything at once. The receiver applied each burst as one jump, so the pointer
moved like 20 fps however smooth the mouse. The smoother keeps what has arrived but not yet been
shown, and its own thread hands a share of it to the injector every TICK seconds, so a burst
drains over about the gap it followed. The window follows the gaps: it rises at once to a long
one and falls back with a half-life of HALF_LIFE seconds, so an even link adds only MIN_WINDOW.

The receiver smooths only motion away from every wall and armed edge, and flushes what is pending
before anything else it injects (a click, a key, a move near an edge), so those land where the
pointer would have been without smoothing.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, Tuple

LOGGER = logging.getLogger(__name__)
TICK = 0.004
MIN_WINDOW = 0.008
MAX_WINDOW = 0.060
HALF_LIFE = 1.0
# A pause longer than this is the mouse at rest, not the link: it does not widen the window.
REST = 0.25


def _whole(value: float) -> int:
    """The whole pixels of `value`, toward zero, so the fraction stays pending."""
    return math.trunc(value)


class MotionSmoother:
    """`move(dx, dy)` injects one relative move; `active()` says whether a peer still drives this
    machine (pending motion is dropped when not). `clock` and `sleep` are injected by tests."""

    def __init__(self, move: Callable[[int, int], None], active: Callable[[], bool] = lambda: True,
                 clock=time.perf_counter, sleep=time.sleep, tick: float = TICK) -> None:
        self._move = move
        self._active = active
        self._clock = clock
        self._sleep = sleep
        self._tick = tick
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._x = self._y = 0.0
        self._window = MIN_WINDOW
        self._last_add = None

    @property
    def window(self) -> float:
        return self._window

    def add(self, dx: float, dy: float) -> None:
        """Motion that has arrived, in the pixels the injector moves."""
        now = self._clock()
        with self._lock:
            if self._last_add is not None:
                gap = now - self._last_add
                decayed = self._window * 0.5 ** (gap / HALF_LIFE)
                widened = gap if gap < REST else 0.0
                self._window = min(MAX_WINDOW, max(MIN_WINDOW, decayed, widened))
            self._last_add = now
            self._x += dx
            self._y += dy
        self._wake.set()

    def pending(self) -> Tuple[int, int]:
        """The whole pixels arrived but not yet injected."""
        with self._lock:
            return _whole(self._x), _whole(self._y)

    def flush(self) -> None:
        """Inject everything pending at once: before a click, a key or a move near an edge."""
        with self._lock:
            dx, dy = _whole(self._x), _whole(self._y)
            self._x -= dx
            self._y -= dy
            if dx or dy:
                self._move(dx, dy)

    def drop(self) -> None:
        with self._lock:
            self._x = self._y = 0.0

    def step(self) -> bool:
        """One tick: inject a share of what is pending. False when nothing is."""
        with self._lock:
            if not self._active():
                self._x = self._y = 0.0
                return False
            if abs(self._x) < 1 and abs(self._y) < 1:
                return False
            share = min(1.0, self._tick / self._window)
            dx, dy = _whole(self._x * share), _whole(self._y * share)
            # At least a pixel on any axis with one pending, so a backlog always finishes.
            if dx == 0 and abs(self._x) >= 1:
                dx = 1 if self._x > 0 else -1
            if dy == 0 and abs(self._y) >= 1:
                dy = 1 if self._y > 0 else -1
            self._x -= dx
            self._y -= dy
            self._move(dx, dy)
            return True

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="Beamer-smoother", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.step()
            except Exception:
                # The injector failing must not end the only thread that drains the motion.
                LOGGER.exception("Smoothed motion could not be injected")
                self.drop()
                busy = False
            if busy:
                self._sleep(self._tick)
            else:
                self._wake.wait(REST)
                self._wake.clear()
