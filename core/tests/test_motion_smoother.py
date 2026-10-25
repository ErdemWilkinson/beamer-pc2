"""Smoothing a peer's pointer motion (core.motion_smoother), alone and in the receiver."""

import copy
import unittest

from core import motion_smoother, protocol
from core.tests.responder_harness import B, Initiator, Machine, entry, wait_for


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def smoother(active=lambda: True):
    moves, clock = [], Clock()
    smooth = motion_smoother.MotionSmoother(lambda dx, dy: moves.append((dx, dy)), active=active, clock=clock)
    return smooth, moves, clock


def play(smooth, clock, arrivals, until, tick=0.004):
    """Run `smooth` on `clock` until `until`, adding each (time, dx) of `arrivals` when due;
    the moves made, as (time, dx)."""
    made, arrivals, now = [], list(arrivals), 0.0
    smooth._move = lambda dx, dy: made.append((clock.now, dx))
    while now <= until:
        clock.now = now
        due = sum(dx for at, dx in arrivals if at <= now)
        arrivals = [(at, dx) for at, dx in arrivals if at > now]
        if due:
            smooth.add(due, 0)
        smooth.step()
        now = round(now + tick, 6)
    return made


class SmootherTests(unittest.TestCase):
    def test_bursts_play_out_at_the_hand_s_steady_speed(self):
        # A hand at 1000 px/s whose motion arrives in a burst every 50 ms, as over PC2's Wi-Fi.
        smooth, _moves, clock = smoother()
        arrivals = [(0.05 * (k + 1), 50) for k in range(40)]
        made = play(smooth, clock, arrivals, until=2.3)
        self.assertEqual(2000, sum(dx for _at, dx in made))
        frames = {}
        for at, dx in made:
            if 0.5 <= at < 1.9:
                frames[int(at * 60)] = frames.get(int(at * 60), 0) + dx
        per_frame = [frames.get(frame, 0) for frame in range(31, 113)]
        # 16.7 px a 60 Hz frame; applied as it arrived, every third frame took the whole 50.
        self.assertGreaterEqual(min(per_frame), 12)
        self.assertLessEqual(max(per_frame), 22)

    def test_the_delay_covers_the_longest_recent_gap(self):
        smooth, _moves, clock = smoother()
        smooth.add(0, 0)
        clock.now = 0.050
        smooth.add(100, 0)
        self.assertAlmostEqual(0.050 * motion_smoother.DELAY_MARGIN, smooth.delay)

    def test_an_even_link_adds_only_the_least_delay(self):
        smooth, _moves, clock = smoother()
        for step in range(100):
            clock.now = step * 0.002
            smooth.add(2, 1)
        self.assertEqual(motion_smoother.MIN_DELAY, smooth.delay)

    def test_the_delay_falls_back_once_the_link_is_even_again(self):
        smooth, _moves, clock = smoother()
        smooth.add(0, 0)
        clock.now = 0.080
        smooth.add(1, 0)
        for step in range(1, 5000):
            clock.now = 0.080 + step * 0.002
            smooth.add(1, 0)
        self.assertLess(smooth.delay, 0.030)

    def test_a_pause_of_the_hand_is_not_a_stall_of_the_link(self):
        smooth, _moves, clock = smoother()
        smooth.add(1, 0)
        clock.now = 2.0
        smooth.add(1, 0)
        self.assertEqual(motion_smoother.MIN_DELAY, smooth.delay)

    def test_flush_shows_everything_at_once_and_keeps_the_fraction(self):
        smooth, moves, _clock = smoother()
        smooth.add(7.5, -3.25)
        smooth.flush()
        self.assertEqual([(7, -3)], moves)
        self.assertEqual((0, 0), smooth.pending())

    def test_motion_is_dropped_once_no_peer_drives_this_machine(self):
        driven = [True]
        smooth, moves, _clock = smoother(active=lambda: driven[0])
        smooth.add(50, 50)
        driven[0] = False
        self.assertFalse(smooth.step())
        self.assertEqual([], moves)
        self.assertEqual((0, 0), smooth.pending())


class ReceiverSmoothingTests(unittest.TestCase):
    def owner(self):
        machine = Machine([entry(B, "Driver")]).start()
        self.addCleanup(machine.stop)
        link = Initiator(machine, B)
        self.addCleanup(link.close)
        link.handshake()
        route = link.take()
        self.assertEqual(link.answer(route)[0], protocol.MSG_ACCEPT)
        machine.responder.input_scale = None
        smooth, _moves, _clock = smoother()
        smooth._move = machine.injector.inject_mouse_move
        machine.responder.smoother = smooth
        return machine, link, smooth

    def path(self, link, *samples):
        message = copy.deepcopy(protocol.mousemove_msg(sum(dx for dx, _dy in samples), sum(dy for _dx, dy in samples)))
        message["data"]["samples"] = [list(sample) for sample in samples]
        return link.input(message)

    def moves(self, machine):
        return [call for call in machine.injector.seen() if call[0] == "move"]

    def test_free_space_motion_waits_in_the_smoother(self):
        machine, link, smooth = self.owner()
        seq = self.path(link, (3, 0), (2, 1))
        self.assertIsNotNone(link.acked(seq))
        self.assertEqual([], self.moves(machine))
        self.assertEqual((5, 1), smooth.pending())

    def test_a_click_lands_where_the_pointer_would_have_been(self):
        machine, link, smooth = self.owner()
        self.assertIsNotNone(link.acked(self.path(link, (3, 0), (2, 1))))
        pressed = link.input({"type": "mousedown", "data": {"button": "left"}})
        self.assertIsNotNone(link.acked(pressed))
        wait_for(lambda: any(call[0] == "button" for call in machine.injector.seen()))
        calls = [call for call in machine.injector.seen() if call[0] in ("move", "button")]
        self.assertEqual([("move", 5, 1), ("button", "left", True)], calls[:2])
        self.assertEqual((0, 0), smooth.pending())


if __name__ == "__main__":
    unittest.main()
