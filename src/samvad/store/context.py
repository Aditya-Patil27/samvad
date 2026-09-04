# OWNER: P3 -- see docs/WORK.md. Do not edit if you are not P3.
"""Token accounting and backpressure.

`used` comes from the token-counting endpoint, NOT an estimate. The whole
backpressure mechanism is worthless if the number is a guess.

Backpressure: a sender seeing used/limit > 0.9 on its peer switches to refs
and summaries only. Advisory -- a peer that ignores it just runs out of
context, which is its own problem.
"""

BACKPRESSURE_THRESHOLD = 0.9


class ContextTracker:
    def used(self) -> int:
        raise NotImplementedError

    def limit(self) -> int:
        raise NotImplementedError

    def should_send_full(self, peer) -> bool:
        raise NotImplementedError
