"""Fallback implementations of each layer's neighbours.

docs/WORK.md frames these as a courtesy that unblocks teammates. Read them the
other way round as well: they are **insurance**. If P3's blob store has not
landed by demo day, the dict version below still completes a round trip; if
P2's agent has not landed, StubAgent still answers a task_request. There is
always something to show.

These live in tests/ deliberately. They are not `src/samvad/` code, they belong
to no owner's layer, and nothing here should ever be imported by production
code. When the real implementation arrives, delete the corresponding fake --
do not let two versions drift.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


def _artifact_ref(ref: str, kind: str, summary: str, tokens: int):
    """Build a protocol.ArtifactRef if it exists yet, else a stand-in.

    Lets the fakes work both before and after the week-1 protocol session.
    """
    try:
        from samvad.protocol import ArtifactRef

        return ArtifactRef(ref=ref, kind=kind, summary=summary, tokens=tokens)
    except (ImportError, AttributeError):
        return _StandInRef(ref=ref, kind=kind, summary=summary, tokens=tokens)


@dataclass(frozen=True)
class _StandInRef:
    ref: str
    kind: str
    summary: str
    tokens: int


class DictBlobStore:
    """Stands in for P3's `store/blobs.py`.

    Content-addressed the same way the real one must be: the same bytes stored
    twice produce one entry and the same ref. That property is the point of the
    layer, so the fake has to honour it or it is not a useful fallback.
    """

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, data: bytes, kind: str):
        ref = "sha256:" + hashlib.sha256(data).hexdigest()
        self._blobs.setdefault(ref, data)
        return _artifact_ref(
            ref=ref,
            kind=kind,
            summary=f"{kind}, {len(data)} bytes",
            # NOT a real token count. The real ContextTracker must use the
            # token-counting endpoint -- docs/PROTOCOL.md is explicit that an
            # estimate makes backpressure worthless. This is a placeholder that
            # exists only so the field is populated.
            tokens=max(1, len(data) // 4),
        )

    def get(self, ref: str) -> bytes | None:
        return self._blobs.get(ref)

    def has(self, ref: str) -> bool:
        return ref in self._blobs


@dataclass
class RecordingInbox:
    """Stands in for P1's `FakeInbox`. Records rather than dispatches.

    Also counts calls, which is what the idempotency contract test needs: a
    duplicate message_id must leave `calls` at 1, not 2.
    """

    received: list[Any] = field(default_factory=list)

    async def __call__(self, msg: Any) -> None:
        self.received.append(msg)

    @property
    def calls(self) -> int:
        return len(self.received)

    def ids(self) -> list[str]:
        return [getattr(m, "message_id", None) or m["message_id"] for m in self.received]


@dataclass
class StubUsage:
    input: int = 100
    output: int = 40
    cache_read: int = 0
    model: str = "mock"


@dataclass
class StubCompletion:
    text: str
    usage: StubUsage = field(default_factory=StubUsage)


class StubLLM:
    """Stands in for P2's `llm/mock.py`. Zero cost, no network, deterministic.

    `replies` is consumed in order; once exhausted it repeats the last one, so a
    test never hangs waiting for a response it forgot to queue.
    """

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies) if replies else ["ok"]
        self.prompts: list[Any] = []

    async def complete(self, prompt: Any) -> StubCompletion:
        self.prompts.append(prompt)
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return StubCompletion(text=text)

    @property
    def call_count(self) -> int:
        return len(self.prompts)
