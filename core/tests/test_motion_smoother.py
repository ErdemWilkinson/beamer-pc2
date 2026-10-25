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


class SmootherTests(unittest.TestCase):
    def test_a_burst_glides_out_instead_of_jumping(self):
        smooth, moves, clock = smoother()
        smooth.add(0, 0)
        clock.now = 0.050  # the link was quiet for 50 ms, then everything came at once
        smooth.add(100, 0)
        self.assertAlmostEqual(0.050, smooth.window)
        while smooth.step():
            pass
        self.assertGreater(len(moves), 10)
        self.assertLess(max(dx for dx, _dy in moves), 20)
        self.assertEqual(100, sum(dx for dx, _dy in moves))

    def test_an_even_link_adds_only_the_least_delay(self):
        smooth, moves, clock = smoother()
        for step in range(100):
            clock.now = step * 0.002
            smooth.add(2, 1)
        self.assertEqual(motion_smoother.MIN_WINDOW, smooth.window)

    def test_the_window_falls_back_once_the_link_is_even_again(self):
        smooth, _moves, clock = smoother()
        smooth.add(0, 0)
        clock.now = 0.060
        smooth.add(1, 0)
        for step in range(1, 1500):
            clock.now = 0.060 + step * 0.002
            smooth.add(1, 0)
        self.assertLess(smooth.window, 0.020)

    def test_a_pause_of_the_hand_is_not_a_stall_of_the_link(self):
        smooth, _moves, clock = smoother()
        smooth.add(1, 0)
        clock.now = 2.0
        smooth.add(1, 0)
        self.assertEqual(motion_smoother.MIN_WINDOW, smooth.window)

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
