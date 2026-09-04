# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""Budget conservation and the circuit breaker.

THE INVARIANT: a child can never hold more than its parent gave it. Slices
sum to <= the parent's. This is what terminates recursion -- a branch that
cannot afford one API call cannot spawn, and answers spawn_refused. There is
no separate fork-bomb guard because there does not need to be one.

DECREMENT BEFORE THE CALL, never after. If the remainder will not cover it,
return budget_exhausted and do not call.

Recursive spawning against a live key is the one way this project costs real
money. These guards are not optional.
"""
from __future__ import annotations

import os
import time

from samvad.protocol import Budget

#: Money is sliced in integer micro-dollars, never in floats.
#: 0.1 + 0.2 != 0.3 in binary floating point, and "slices sum to <= the
#: parent's" is an invariant that must hold exactly -- not to within a rounding
#: error that compounds at every level of a spawn tree.
MICRO = 1_000_000

#: A call too cheap to be real. Used as the floor for "can this branch afford
#: to do anything at all", so a slice of $0.000001 refuses instead of spawning
#: a child that will immediately answer budget_exhausted.
MIN_VIABLE_USD = 0.001


def _micros(usd: float) -> int:
    return int(usd * MICRO)


def slice_budget(parent: Budget, n: int) -> list[Budget]:
    """Divide a parent's budget among n children. Slices must sum to <= parent.

    Integer division with the remainder kept BY THE PARENT, deliberately:

      - the sum is provably <= the parent's, with no float drift to argue about
      - the parent keeps a little, which it needs -- it still has to receive n
        results and merge them, and a parent that gave away every cent cannot
        pay for its own last turn

    Raises ValueError for n < 1: slicing a budget zero ways is a caller bug,
    and silently returning [] would let a fan-out of nothing look successful.
    """
    if n < 1:
        raise ValueError(f"cannot slice a budget {n} ways")

    usd_each = _micros(parent.usd_remaining) // n
    turns_each = parent.turns_remaining // n
    return [
        Budget(usd_remaining=usd_each / MICRO, turns_remaining=turns_each) for _ in range(n)
    ]


def can_afford(budget: Budget, estimated_usd: float) -> bool:
    """True if this budget covers one more call of roughly this size.

    Checked BEFORE the call, never after. A branch that cannot afford one call
    must not make it -- that is what stops a recursive spawn tree from
    discovering its own limit by spending past it.

    Turns count too: a branch with money but no turns left is finished, and
    docs/PROTOCOL.md reaches `abandoned` on either running out.
    """
    if budget.turns_remaining < 1:
        return False
    return _micros(budget.usd_remaining) >= _micros(max(estimated_usd, MIN_VIABLE_USD))


class CircuitBreaker:
    """Hard per-hour ceiling, checked BEFORE the API call. On trip the agent
    returns budget_exhausted -- a protocol state peers can reason about, not
    an exception that kills the process."""

    def __init__(self, max_usd_per_hour: float | None = None, window_seconds: float = 3600.0) -> None:
        self.max_usd_per_hour = (
            max_usd_per_hour
            if max_usd_per_hour is not None
            else float(os.environ.get("SAMVAD_MAX_USD_PER_HOUR", "1.00"))
        )
        self.window_seconds = window_seconds
        self._spend: list[tuple[float, float]] = []

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._prune_before = cutoff
        self._spend = [(t, usd) for t, usd in self._spend if t >= cutoff]

    def spent(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._prune(now)
        return sum(usd for _, usd in self._spend)

    def record(self, usd: float, now: float | None = None) -> None:
        """Call this immediately after a call returns, with what it actually cost."""
        self._spend.append((time.monotonic() if now is None else now, usd))

    def check(self, estimated_usd: float = 0.0, now: float | None = None) -> bool:
        """True if one more call of this size stays under the hourly ceiling.

        Monotonic clock, not wall clock: this is a rate limit, and a laptop
        waking from sleep or an NTP correction must not hand the breaker a
        negative interval and reset the window.
        """
        return self.spent(now) + max(estimated_usd, 0.0) <= self.max_usd_per_hour
