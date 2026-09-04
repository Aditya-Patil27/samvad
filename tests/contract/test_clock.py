"""Contract: logical ordering. Owner P1.

Deliberately NOT gated on protocol.py. A Lamport clock is just an integer with
two rules -- it does not touch the envelope, so it can be built and proved
before the week-1 protocol session.

Four devices means four wall clocks that disagree. Order log entries by
(lamport, sender), never by timestamp.
"""
import pytest

from samvad.clock import LamportClock


def test_observe_takes_max_plus_one():
    """local = max(local, remote) + 1 -- whichever side is ahead."""
    assert LamportClock(start=5).observe(9) == 10
    assert LamportClock(start=5).observe(2) == 6


def test_observe_advances_even_when_remote_is_behind():
    """A stale remote must not stall the local clock."""
    clock = LamportClock(start=10)
    assert clock.observe(1) == 11
    assert clock.value == 11


def test_tick_is_monotonic_and_never_repeats():
    """Every send advances the clock; two sends never share a value."""
    clock = LamportClock()
    seen = [clock.tick() for _ in range(5)]
    assert seen == [1, 2, 3, 4, 5]
    assert len(set(seen)) == 5


def test_value_reflects_the_last_event_without_advancing():
    """Reading is not an event. Only tick() and observe() move the clock."""
    clock = LamportClock(start=3)
    assert clock.value == 3
    assert clock.value == 3
    clock.tick()
    assert clock.value == 4


def test_causality_holds_across_a_round_trip():
    """The property the whole thing exists for: if A sent before B received,
    B's clock is strictly ahead of the value A stamped."""
    a, b = LamportClock(), LamportClock()
    stamped_by_a = a.tick()
    at_b = b.observe(stamped_by_a)
    assert at_b > stamped_by_a

    stamped_by_b = b.tick()
    assert a.observe(stamped_by_b) > stamped_by_b


def test_a_negative_remote_is_rejected():
    """lamport is `int >= 0` in docs/PROTOCOL.md. observe() reads off the wire,
    so it is a boundary and validates."""
    with pytest.raises(ValueError):
        LamportClock().observe(-1)


def test_start_cannot_be_negative():
    with pytest.raises(ValueError):
        LamportClock(start=-1)
