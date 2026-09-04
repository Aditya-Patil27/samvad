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


def slice_budget(parent, n: int) -> list:
    """Divide a parent's budget among n children. Slices must sum to <= parent."""
    raise NotImplementedError


def can_afford(budget, estimated_usd: float) -> bool:
    raise NotImplementedError


class CircuitBreaker:
    """Hard per-hour ceiling, checked BEFORE the API call. On trip the agent
    returns budget_exhausted -- a protocol state peers can reason about, not
    an exception that kills the process."""

    def check(self) -> bool:
        raise NotImplementedError
