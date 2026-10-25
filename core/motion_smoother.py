"""Pointer motion from a peer, played out on the hand's own timeline instead of as it arrives.

Over a jittery Wi-Fi link the moves of a steadily moving mouse arrive in bursts: nothing for
20-80 ms, then everything at once. The receiver applied each burst as one jump, so the pointer
moved like 20 fps however smooth the mouse.

This is a jitter buffer. Motion that arrives after a gap was made during that gap, so each
arrival is spread evenly over the gap before it (at most MAX_DELAY; after a rest of the hand,
over one TICK). That rebuilds the hand's timeline, and the pointer follows it `delay` seconds
behind, moved every TICK seconds by whatever of the timeline that stretch covers. A stall
shorter than the delay is played straight through at the hand's speed. The delay follows the
link: it rises at once to the longest recent gap (with a margin) and falls back with a half-life
of HALF_LIFE seconds, between MIN_DELAY and MAX_DELAY.

The receiver smooths only motion away from every wall and armed edge, and flushes what is
pending before anything else it injects (a click, a key, a move near an edge), so those land
where the pointer would have been without smoothing.
"""

from __future__ import annotations

import collections
import logging
import math
import sys
import threading
import time
from typing import Callable, Tuple

LOGGER = logging.getLogger(__name__)
TICK = 0.004
MIN_DELAY = 0.020
MAX_DELAY = 0.100
DELAY_MARGIN = 1.2
HALF_LIFE = 2.0
# A pause longer than this is the hand at rest, not the link: it is not spread over, and it does
# not lengthen the delay.
REST = 0.25
STATS_EVERY = 10.0


def _whole(value: float) -> int:
    """The whole pixels of `value`, toward zero, so the fraction stays pending."""
    return math.trunc(value)


def _fine_timer(enable: bool) -> None:
    """Ask Windows for 1 ms timer ticks while motion plays, so each TICK is a TICK."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        winmm = ctypes.windll.winmm
        (winmm.timeBeginPeriod if enable else winmm.timeEndPeriod)(1)
    except (AttributeError, OSError):
        LOGGER.debug("Could not change the timer resolution", exc_info=True)


class _Stretch:
    """Motion (dx, dy) made evenly over [start, end] on the hand's timeline; `done` is the share
    already moved."""

    __slots__ = ("start", "end", "dx", "dy", "done")

    def __init__(self, start, end, dx, dy):
        self.start, self.end, self.dx, self.dy, self.done = start, end, dx, dy, 0.0

    def share(self, until):
        if until >= self.end:
            return 1.0
        return max(0.0, (until - self.start) / (self.end - self.start))


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
        self._stretches = collections.deque()
        self._x = self._y = 0.0
        self._out_x = self._out_y = 0.0
        self._delay = MIN_DELAY
        self._last_add = None
        self._played_to = float("-inf")
        # For the log, every STATS_EVERY seconds of play: what arrived, how evenly the steps ran,
        # and how often the buffer ran dry while motion was still owed.
        self._stats = {"arrivals": 0, "steps": 0, "dry": 0, "longest_step": 0.0, "longest_gap": 0.0}
        self._stats_at = None
        self._last_step_at = None

    @property
    def delay(self) -> float:
        return self._delay

    def add(self, dx: float, dy: float) -> None:
        """Motion that has arrived, in the pixels the injector moves."""
        now = self._clock()
        with self._lock:
            gap = now - self._last_add if self._last_add is not None else REST
            spread = min(gap, MAX_DELAY) if gap < REST else self._tick
            self._stats["arrivals"] += 1
            if gap < REST:
                self._stats["longest_gap"] = max(self._stats["longest_gap"], gap)
            self._delay = min(MAX_DELAY, max(MIN_DELAY, self._delay * 0.5 ** (gap / HALF_LIFE),
                                             spread * DELAY_MARGIN))
            self._last_add = now
            self._stretches.append(_Stretch(now - spread, now, dx, dy))
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
            self._out_x = self._out_y = 0.0
            self._stretches.clear()
            self._played_to = self._clock() - self._delay
            if dx or dy:
                self._move(dx, dy)

    def drop(self) -> None:
        with self._lock:
            self._x = self._y = self._out_x = self._out_y = 0.0
            self._stretches.clear()

    def step(self) -> bool:
        """Move the pointer along the timeline up to `delay` ago. False when nothing is pending."""
        now = self._clock()
        with self._lock:
            if not self._active():
                self._x = self._y = self._out_x = self._out_y = 0.0
                self._stretches.clear()
                return False
            if not self._stretches:
                return False
            if self._last_step_at is not None and now - self._last_step_at < REST:
                self._stats["longest_step"] = max(self._stats["longest_step"], now - self._last_step_at)
            self._last_step_at = now
            self._stats["steps"] += 1
            until = max(self._played_to, now - self._delay)
            self._played_to = until
            if until <= self._stretches[0].start and now - self._stretches[-1].end > self._delay:
                # Owed motion, none of it due yet, and nothing new for longer than the delay:
                # the link stalled past what the buffer covers.
                self._stats["dry"] += 1
            for stretch in self._stretches:
                if stretch.start >= until:
                    break
                share = stretch.share(until)
                self._out_x += stretch.dx * (share - stretch.done)
                self._out_y += stretch.dy * (share - stretch.done)
                stretch.done = share
            while self._stretches and self._stretches[0].done >= 1.0:
                self._stretches.popleft()
            dx, dy = _whole(self._out_x), _whole(self._out_y)
            self._out_x -= dx
            self._out_y -= dy
            self._x -= dx
            self._y -= dy
            if not self._stretches:
                # The last of it: the fraction left over goes with the next motion.
                self._out_x = self._out_y = 0.0
            if dx or dy:
                self._move(dx, dy)
            return True

    def start(self) -> None:
        if self._thread is None:
            _fine_timer(True)
            self._thread = threading.Thread(target=self._run, name="Beamer-smoother", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread is not None and not self._stop.is_set():
            _fine_timer(False)
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
            self._report()
            if busy:
                self._sleep(self._tick)
            else:
                self._last_step_at = None
                self._wake.wait(REST)
                self._wake.clear()

    def _report(self) -> None:
        now = self._clock()
        if self._stats_at is None:
            self._stats_at = now
            return
        if now - self._stats_at < STATS_EVERY:
            return
        with self._lock:
            stats, self._stats_at = dict(self._stats), now
            self._stats.update(arrivals=0, steps=0, dry=0, longest_step=0.0, longest_gap=0.0)
        if stats["steps"]:
            LOGGER.info("Smoothed motion: %d arrivals (longest gap %.0f ms), %d steps (longest %.1f ms), "
                        "%d dry, delay %.0f ms", stats["arrivals"], stats["longest_gap"] * 1000, stats["steps"],
                        stats["longest_step"] * 1000, stats["dry"], self._delay * 1000)
