# OWNER: P3 -- see docs/WORK.md. Do not edit if you are not P3.
"""Token accounting and backpressure.

`used` comes from the token-counting endpoint, NOT an estimate. The whole
backpressure mechanism is worthless if the number is a guess.

Backpressure: a sender seeing used/limit > 0.9 on its peer switches to refs
and summaries only. Advisory -- a peer that ignores it just runs out of
context, which is its own problem.
"""
from __future__ import annotations

from typing import Any

BACKPRESSURE_THRESHOLD = 0.9

#: Context window per model. A window this tracker does not know is not
#: guessed at -- see `limit`.
#:
#: These were WRONG in the first version of this file: 200K was assumed for
#: every Claude model, when Opus 5 and Sonnet 5 carry 1M. Backpressure trips at
#: 90% of this number, so the mesh would have started refusing to send full
#: content at 180K while 820K of window sat unused -- and measurement 4, whose
#: whole subject is how much context the ref mechanism saves, would have been
#: measuring the wrong ceiling.
#:
#: Note Haiku 4.5 really is 200K. The peers in config/peers.example.yaml are
#: deliberately heterogeneous, so one node in a four-node mesh has a window
#: five times smaller than its neighbours -- backpressure exists for exactly
#: that asymmetry, and a uniform table would have hidden it.
MODEL_LIMITS: dict[str, int] = {
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "ollama:qwen2.5-coder": 32_768,
}

#: Conservative: an unknown model is assumed to have the SMALLEST window in the
#: table, not the largest. Over-estimating a window means discovering it is
#: full mid-task, which no retry fixes.
DEFAULT_LIMIT = 200_000


class ContextTracker:
    """What this agent has actually spent of its own window.

    Fed from the `cost.input` the backend REPORTED, never from a local
    estimate. That distinction is the whole reason this class exists: the
    docstring above and docs/PROTOCOL.md both insist on it, because
    backpressure computed from a guess would trip late -- and the failure it
    exists to prevent is an agent discovering its window is full mid-task,
    which no amount of retrying fixes.

    The one number in the store that IS estimated is `ArtifactRef.tokens`, in
    blobs.py, which has to be known before a call rather than after one. It is
    labelled there and is deliberately not used here.
    """

    def __init__(self, model: str = "claude-opus-5", limit: int | None = None) -> None:
        self.model = model
        self._limit = limit
        self._used = 0

    def used(self) -> int:
        return self._used

    def limit(self) -> int:
        return self._limit if self._limit is not None else MODEL_LIMITS.get(
            self.model, DEFAULT_LIMIT
        )

    def observe(self, cost: Any) -> int:
        """Record what a call actually consumed, from the backend's own report.

        `input` is the whole prompt the model read, cache hits included: a
        cached prefix is cheaper in money but occupies exactly as much of the
        window. Subtracting cache_read here would make the tracker report a
        window emptier than it is, which is the one direction that matters.
        """
        if cost is not None:
            self._used = max(self._used, int(getattr(cost, "input", 0)))
        return self._used

    def pressure(self) -> float:
        return self._used / self.limit()

    def should_send_full(self, peer: Any) -> bool:
        """False when the PEER is above 90% -- send refs and summaries instead.

        Note whose number this is: the receiver's, carried on the envelope in
        `context`. Backpressure is a courtesy paid by the sender using state
        the receiver published, which is why the field is on every message.

        Advisory, never enforced. A peer that ignores it runs out of context,
        which is its own problem -- but a sender that ignores it causes someone
        else's failure, which is not.
        """
        used = getattr(peer, "used", None)
        limit = getattr(peer, "limit", None)
        if used is None or not limit:
            # Unknown peer state: send full. Silently degrading to summaries
            # would drop real content for a peer that had plenty of room.
            return True
        return (used / limit) <= BACKPRESSURE_THRESHOLD

    def would_exhaust(self, additional_tokens: int) -> bool:
        """True if hydrating this artifact would push past the window.

        The check a receiver runs before fetching a blob: `ArtifactRef.tokens`
        is carried precisely so this decision can be made before paying for it.
        """
        return self._used + additional_tokens > self.limit()
