"""Pointer motion from a peer, played out at the hand's speed instead of as it arrives.

Over a jittery Wi-Fi link the moves of a steadily moving mouse arrive in bursts: nothing for
20-80 ms, then the dozens of small messages sent meanwhile, all at once. The receiver applied
them as they came, so the pointer jumped every burst and looked like 20 fps however smooth the
mouse.

This is a jitter buffer. What arrives joins a queue, kept in order so the pointer follows the
hand's path. The pointer is moved every TICK seconds along that queue at the hand's speed: the
path length that arrived over the last SPEED_WINDOW seconds, eased over EASE seconds so bursts
do not make it pulse, and corrected to keep about `delay` seconds of motion in hand, which lets a
stall shorter than the delay play straight through. The delay follows the link: a gap between
arrivals while the hand is moving (a stall; a gap while it is still is the hand resting) sets it
to that gap with a margin, and it falls back with a half-life of HALF_LIFE seconds, between
MIN_DELAY and MAX_DELAY.

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
# Tuned in simulation against PC2's link (a hand moving at 1000 px/s in bursts, 50 ms stalls):
# the first values here (0.25, 0.10, 0.15, 90 ms, 1.2) were smooth but left the pointer 82 ms
# behind the hand, "as if carrying twenty kilos"; these keep it 56 ms behind, about as smooth.
SPEED_WINDOW = 0.16
EASE = 0.04
CORRECTION = 0.08
MIN_DELAY = 0.020
MAX_DELAY = 0.060
DELAY_MARGIN = 0.9
HALF_LIFE = 1.0
# A gap longer than STALL while the hand moves was a stall of the link if, soon after the arrival
# that ends it (half the gap, at most BURST), at least half the motion the hand would have made
# meanwhile comes in: the messages held, released together (a burst a slow receiver works through
# one by one, or merged into one). A gap followed by little was the hand resting.
STALL = 0.008
BURST = 0.020
# The hand counts as moving above this speed, in pixels a second.
MOVING = 30.0
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


class MotionSmoother:
    """`move(dx, dy)` places the pointer that far on; `active()` says whether a peer still drives
    this machine (pending motion is dropped when not). `clock` and `sleep` are injected by tests."""

    def __init__(self, move: Callable[[int, int], None], active: Callable[[], bool] = lambda: True,
                 clock=time.perf_counter, sleep=time.sleep, tick: float = TICK,
                 max_delay: float = MAX_DELAY) -> None:
        self._move = move
        # The most motion held back to ride out a stall: smoother when longer, quicker when shorter.
        self._max_delay = max(MIN_DELAY, max_delay)
        self._active = active
        self._clock = clock
        self._sleep = sleep
        self._tick = tick
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._queue = collections.deque()      # [dx, dy] still to move, oldest first
        self._arrived = collections.deque()    # (time, path length) over SPEED_WINDOW
        self._x = self._y = 0.0                # the queue's sum
        self._out_x = self._out_y = 0.0        # moved in fractions, not yet whole pixels
        self._speed = 0.0
        self._delay = MIN_DELAY
        self._last_add = None
        self._suspect = None                   # a gap while moving: a stall if its motion follows
        self._last_step = None
        self._stats = {"arrivals": 0, "steps": 0, "dry": 0, "longest_step": 0.0, "longest_stall": 0.0}
        self._reasons = {}
        self._stats_at = None

    @property
    def delay(self) -> float:
        return self._delay

    def add(self, dx: float, dy: float) -> None:
        """Motion that has arrived, in the pixels the pointer moves."""
        now = self._clock()
        with self._lock:
            self._stats["arrivals"] += 1
            length = math.hypot(dx, dy)
            if self._last_add is not None:
                gap = now - self._last_add
                delay = self._delay * 0.5 ** (gap / HALF_LIFE)
                if gap > STALL and self._speed > MOVING and gap < 4 * self._max_delay:
                    # [the gap, the motion it should have held, what has come since, until when]
                    self._suspect = [gap, self._speed * gap, 0.0, now + min(BURST, gap / 2)]
                if self._suspect is not None:
                    if now > self._suspect[3]:
                        self._suspect = None
                    else:
                        self._suspect[2] += length
                        if self._suspect[2] >= 0.5 * self._suspect[1]:
                            self._stats["longest_stall"] = max(self._stats["longest_stall"], self._suspect[0])
                            delay = max(delay, self._suspect[0] * DELAY_MARGIN)
                            self._suspect = None
                self._delay = min(self._max_delay, max(MIN_DELAY, delay))
            self._last_add = now
            self._queue.append([float(dx), float(dy)])
            self._arrived.append((now, length))
            self._x += dx
            self._y += dy
        self._wake.set()

    def note(self, why: str) -> None:
        """What the receiver did with a motion message: "smoothed", or why it went straight in."""
        with self._lock:
            self._reasons[why] = self._reasons.get(why, 0) + 1

    def pending(self) -> Tuple[int, int]:
        """The whole pixels arrived but not yet moved."""
        with self._lock:
            return _whole(self._x), _whole(self._y)

    def flush(self) -> None:
        """Move everything pending at once: before a click, a key or a move near an edge."""
        with self._lock:
            dx, dy = _whole(self._x), _whole(self._y)
            self._x -= dx
            self._y -= dy
            self._queue.clear()
            self._out_x = self._out_y = 0.0
            if dx or dy:
                self._move(dx, dy)

    def drop(self) -> None:
        with self._lock:
            self._clear()

    def _clear(self) -> None:
        self._queue.clear()
        self._arrived.clear()
        self._x = self._y = self._out_x = self._out_y = 0.0
        self._speed = 0.0

    def _take(self, distance: float) -> Tuple[float, float]:
        """`distance` along the queued path, from its oldest end."""
        moved_x = moved_y = 0.0
        while self._queue and distance > 0:
            part = self._queue[0]
            length = math.hypot(part[0], part[1])
            if length <= distance:
                moved_x += part[0]
                moved_y += part[1]
                distance -= length
                self._queue.popleft()
                continue
            share = distance / length
            moved_x += part[0] * share
            moved_y += part[1] * share
            part[0] -= part[0] * share
            part[1] -= part[1] * share
            distance = 0.0
        return moved_x, moved_y

    def step(self) -> bool:
        """Move the pointer for the time since the last step. False when nothing is pending."""
        now = self._clock()
        with self._lock:
            if not self._active():
                self._clear()
                return False
            while self._arrived and now - self._arrived[0][0] > SPEED_WINDOW:
                self._arrived.popleft()
            owed = sum(math.hypot(part[0], part[1]) for part in self._queue)
            if owed < 0.5:
                # The last of it, fractions included, so the pointer ends where the hand did.
                dx, dy = round(self._out_x + self._x), round(self._out_y + self._y)
                self._queue.clear()
                self._x = self._y = self._out_x = self._out_y = 0.0
                self._last_step = None
                if dx or dy:
                    self._move(dx, dy)
                return False
            elapsed = self._tick if self._last_step is None else min(0.05, now - self._last_step)
            if self._last_step is not None:
                self._stats["longest_step"] = max(self._stats["longest_step"], elapsed)
            self._last_step = now
            self._stats["steps"] += 1
            arriving = sum(length for _at, length in self._arrived) / SPEED_WINDOW
            self._speed += (arriving - self._speed) * min(1.0, elapsed / EASE)
            # The hand's speed, pulled toward keeping `delay` seconds of motion in hand; never below
            # half the hand's speed while something is owed (a stop is what reads as a stutter),
            # and draining what is left once the hand has stopped.
            rate = max(self._speed + (owed - self._speed * self._delay) / CORRECTION,
                       0.5 * self._speed, owed / (4 * CORRECTION))
            distance = min(owed, rate * elapsed)
            if distance <= 0:
                self._stats["dry"] += 1
                return True
            moved_x, moved_y = self._take(distance)
            self._out_x += moved_x
            self._out_y += moved_y
            dx, dy = _whole(self._out_x), _whole(self._out_y)
            self._out_x -= dx
            self._out_y -= dy
            self._x -= moved_x
            self._y -= moved_y
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
                self._wake.wait(0.25)
                self._wake.clear()

    def _report(self) -> None:
        now = self._clock()
        if self._stats_at is None:
            self._stats_at = now
            return
        if now - self._stats_at < STATS_EVERY:
            return
        with self._lock:
            stats, reasons, self._stats_at = dict(self._stats), self._reasons, now
            self._stats.update(arrivals=0, steps=0, dry=0, longest_step=0.0, longest_stall=0.0)
            self._reasons = {}
        if stats["steps"] or reasons:
            LOGGER.info("Smoothed motion: %d arrivals (longest stall %.0f ms), %d steps (longest %.1f ms), "
                        "%d dry, delay %.0f ms; messages: %s", stats["arrivals"], stats["longest_stall"] * 1000,
                        stats["steps"], stats["longest_step"] * 1000, stats["dry"], self._delay * 1000,
                        ", ".join(f"{why} {count}" for why, count in sorted(reasons.items())) or "none")
