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

#: Context window per model, in tokens. Backpressure trips at 90% of this, so
#: a wrong number here does not fail -- it makes the mesh either refuse to send
#: content it had room for, or fill a window it thought was empty.
#:
#: Both directions have already happened in this file. Claude models were
#: entered at 200K when Opus 5 and Sonnet 5 carry 1M (too small, backpressure
#: fired early); ollama models carried qwen3's spec-sheet 40,960 when ollama
#: was actually serving 4096 (too large, backpressure never fired at all).
#: Look the number up, or derive it -- do not remember it.
MODEL_LIMITS: dict[str, int] = {
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    # Haiku 4.5 really is 200K. config/peers.example.yaml is deliberately
    # heterogeneous, so one node in a four-node mesh has a window five times
    # smaller than its neighbours -- backpressure exists for that asymmetry.
    "claude-haiku-4-5": 200_000,
}

#: Hosted free tiers (groq:, nvidia:) do not publish one number we control, and
#: providers cap context below the model's native window on free plans. 8K is
#: deliberately pessimistic: under-estimating means sending refs when full
#: content would have fit, which is recoverable. Over-estimating means the
#: request is rejected or silently truncated mid-task, which is not.
#: Override per peer with ContextTracker(limit=...) once you have measured it.
HOSTED_DEFAULT_LIMIT = 8_192

#: Anything else. Not the largest window in the table -- the smallest sane one.
DEFAULT_LIMIT = 8_192


def window_for(model: str) -> int:
    """The window a model is actually being served with.

    Local ollama models are DERIVED, not tabulated: this project sends
    `num_ctx` explicitly on every request, so the served window is exactly
    DEFAULT_NUM_CTX and no table can drift away from it. Every previous bug in
    this file was a table disagreeing with reality; the fix is to stop keeping
    a table for the one case we control.
    """
    name = (model or "").strip()

    if name.startswith("ollama:"):
        from samvad.llm.ollama import DEFAULT_NUM_CTX

        return DEFAULT_NUM_CTX

    if name.startswith(("groq:", "nvidia:")):
        return HOSTED_DEFAULT_LIMIT

    return MODEL_LIMITS.get(name, DEFAULT_LIMIT)


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
        return self._limit if self._limit is not None else window_for(self.model)

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
