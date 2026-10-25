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


def deliveries(stall, seconds, speed=1000.0, every=0.002):
    """A sender's messages, one every `every` seconds of `speed` px/s, each held by the link for
    `stall(sent)` and delivered in order, as TCP does: what a stall holds arrives back to back."""
    out, last, sent = [], 0.0, 0.0
    while sent < seconds:
        last = max(last, sent + stall(sent))
        out.append((last, speed * every))
        sent = round(sent + every, 6)
    return out


def play(smooth, clock, arrivals, until, tick=0.004):
    """Run `smooth` on `clock` until `until`, adding each (time, dx) of `arrivals` on its own at
    its time; the moves made, as (time, dx)."""
    made, now, index = [], 0.0, 0
    smooth._move = lambda dx, dy: made.append((clock.now, dx))
    while now <= until:
        while index < len(arrivals) and arrivals[index][0] <= now:
            clock.now = arrivals[index][0]
            smooth.add(arrivals[index][1], 0)
            index += 1
        clock.now = now
        smooth.step()
        now = round(now + tick, 6)
    return made


def per_frame(made, start, end):
    frames = {}
    for at, dx in made:
        if start <= at < end:
            frames[int(at * 60)] = frames.get(int(at * 60), 0) + dx
    return [frames.get(frame, 0) for frame in range(int(start * 60) + 1, int(end * 60))]


class SmootherTests(unittest.TestCase):
    def test_bursts_play_out_at_the_hand_s_steady_speed(self):
        # A hand at 1000 px/s whose messages the link releases every 50 ms, as over PC2's Wi-Fi.
        smooth, _moves, clock = smoother()
        made = play(smooth, clock, deliveries(lambda sent: 0.05 - sent % 0.05, 2.0), until=2.6)
        self.assertEqual(2000, sum(dx for _at, dx in made))
        frames = per_frame(made, 0.8, 2.0)
        # 16.7 px a 60 Hz frame; applied as it arrived, every third frame took the whole 50.
        self.assertGreaterEqual(min(frames), 12)
        self.assertLessEqual(max(frames), 26)

    def test_a_stall_while_the_hand_moves_lengthens_the_delay(self):
        smooth, _moves, clock = smoother()
        play(smooth, clock, deliveries(lambda sent: 0.002 if sent < 0.5 else 0.06 - sent % 0.06, 1.0), until=1.0)
        self.assertGreater(smooth.delay, 0.05)
        self.assertLessEqual(smooth.delay, motion_smoother.MAX_DELAY)

    def test_the_longest_delay_can_be_chosen(self):
        smooth = motion_smoother.MotionSmoother(lambda dx, dy: None, clock=Clock(), max_delay=0.03)
        clock = smooth._clock
        play(smooth, clock, deliveries(lambda sent: 0.06 - sent % 0.06, 1.0), until=1.0)
        self.assertLessEqual(smooth.delay, 0.03)

    def test_an_even_link_adds_only_the_least_delay(self):
        smooth, _moves, clock = smoother()
        play(smooth, clock, deliveries(lambda sent: 0.002, 1.0), until=1.0)
        self.assertEqual(motion_smoother.MIN_DELAY, smooth.delay)

    def test_the_delay_falls_back_once_the_link_is_even_again(self):
        smooth, _moves, clock = smoother()
        play(smooth, clock, deliveries(lambda sent: 0.06 - sent % 0.06 if sent < 0.5 else 0.002, 5.0), until=5.0)
        self.assertLess(smooth.delay, 0.030)

    def test_a_rest_of_the_hand_is_not_a_stall_of_the_link(self):
        smooth, _moves, clock = smoother()
        arrivals = [(at, dx) for at, dx in deliveries(lambda sent: 0.002, 0.3)]
        arrivals += [(at + 0.5, dx) for at, dx in deliveries(lambda sent: 0.002, 0.3)]
        play(smooth, clock, arrivals, until=1.0)
        self.assertEqual(motion_smoother.MIN_DELAY, smooth.delay)

    def test_the_pointer_follows_the_hand_s_path(self):
        smooth, moves, clock = smoother()
        smooth.add(30, 0)
        smooth.add(0, 40)
        for step in range(1, 200):
            clock.now = step * 0.004
            smooth.step()
        # Along the right, then down: never cutting the corner diagonally.
        self.assertEqual((30, 40), (sum(dx for dx, _dy in moves), sum(dy for _dx, dy in moves)))
        reached = 0
        for dx, dy in moves:
            if dy:
                self.assertGreaterEqual(reached + dx, 29)
            reached += dx

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

    def test_a_single_move_is_smoothed_too(self):
        # Only merged moves carry a path; while the link keeps up most moves are single ones, and
        # leaving them out left most of the motion jumping (PC2's log, 26-10-2026).
        machine, link, smooth = self.owner()
        self.assertIsNotNone(link.acked(link.move(4, -2)))
        self.assertEqual([], self.moves(machine))
        self.assertEqual((4, -2), smooth.pending())
        self.assertEqual({"smoothed": 1}, smooth._reasons)

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
