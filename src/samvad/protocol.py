# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""The frozen envelope. Spec: docs/PROTOCOL.md

This is the executable form of that document. If the two disagree, the document
is right and this file is a bug -- with the two exceptions recorded below.

NEVER loosen validation to make a test pass. A rejection here is the whole
defence against four assistants confidently building four incompatible
envelopes; a permissive parse turns that loud failure into a silent one that
surfaces at integration instead.

TWO PLACES THE SPEC CONTRADICTS ITSELF -- raise both before tagging
-------------------------------------------------------------------
1. The example envelope carries `sender: "agent_a"` (path depth 0) alongside
   `spawn: {"parent": "agent_a", "depth": 1}`. The field table says depth "must
   equal the path depth" and parent "equals `sender` minus the last segment" --
   the example satisfies neither. The normative table is implemented here and
   the example is treated as wrong. `parent == sender` is accepted at depth 0,
   since "minus the last segment" is empty for a peer and a peer is its own
   root; that also matches what the example shows for parent.

2. `Claim` is {claim, evidence, evidence_type} with no id and no citation
   field, yet `peer_report` is defined as grounded "only if its source is" and
   EXPERIMENTS.md measurement 3 says to follow those chains to their root.
   There is nothing to follow. `experiments/grounding.py` currently infers the
   edge from `reply_to`. Adding `claims[].id` and `claims[].cites` is a MINOR
   bump -- new optional fields, old agents ignore them. Not added here: a
   schema change is the four owners' call, not one person's.

WHAT IS DELIBERATELY NOT ENFORCED HERE
--------------------------------------
Two invariants need conversation history, which a single envelope does not
have. They belong in the receive path (server.py), not in this schema:
  - `root_task` is immutable within a conversation_id
  - a child may not raise `max_depth` above its parent's
This file enforces everything checkable from one message in isolation.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Performative(StrEnum):
    TASK_REQUEST = "task_request"
    TASK_RESULT = "task_result"
    SPAWN_REQUEST = "spawn_request"
    SPAWN_ACK = "spawn_ack"
    SPAWN_REFUSED = "spawn_refused"
    CHILD_RESULT = "child_result"
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNCERTAIN = "uncertain"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    NEEDS_REVISION = "needs_revision"
    UNCERTAIN = "uncertain"
    ABANDONED = "abandoned"


class EvidenceType(StrEnum):
    TEST_OUTPUT = "test_output"      # grounded
    FILE_CONTENT = "file_content"    # grounded
    PEER_REPORT = "peer_report"      # grounded only if its source is
    MODEL_PRIOR = "model_prior"      # NOT grounded -- counted as such


PROTOCOL_VERSION = "2.0"

#: Agent paths: '/'-separated, lowercase, [a-z0-9_] per segment.
AGENT_PATH = re.compile(r"^[a-z0-9_]+(?:/[a-z0-9_]+)*$")

#: Artifact refs: sha256 and 64 hex characters. Nothing else is a ref.
SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Reasons a spawn may be refused. docs/PROTOCOL.md -- performatives.
SPAWN_REFUSED_REASONS = frozenset({"at_capacity", "context_full", "budget_policy", "max_depth"})

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


class _Strict(BaseModel):
    """Base for every envelope part.

    `extra="forbid"` is the executable form of CLAUDE.md's rule: never invent a
    message field. Anything the envelope does not carry goes in `payload` or it
    does not go. Silently accepting an unknown key is how two agents end up
    agreeing on a field that only one of them reads.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)


def depth_of(path: str) -> int:
    """Path depth. A peer is 0, its child 1, a grandchild 2."""
    return path.count("/")


def parent_of(path: str) -> str:
    """The path minus its last segment. A peer is its own parent."""
    return path.rsplit("/", 1)[0] if "/" in path else path


class ArtifactRef(_Strict):
    """A pointer to bytes, never the bytes.

    Four agents holding the same payload is how a 4x200K pool collapses to 200K
    duplicated four times. `tokens` is the price of hydrating, carried so the
    receiver can decide BEFORE it pays.
    """

    ref: str
    kind: str
    summary: str = Field(max_length=200)
    tokens: int = Field(ge=0)

    @field_validator("ref")
    @classmethod
    def _sha256(cls, v: str) -> str:
        if not SHA256_REF.match(v):
            raise ValueError(f"artifact ref must be 'sha256:' + 64 hex chars, got {v!r}")
        return v


class Claim(_Strict):
    """A factual assertion with the evidence behind it.

    Agents label their own claims. Making a model declare its unsupported ones
    is cheap and works far better than it should -- the grounded-to-model_prior
    ratio is measurement 3.
    """

    claim: str
    evidence: str
    evidence_type: EvidenceType

    @property
    def grounded(self) -> bool:
        """True only for directly-grounded evidence.

        `peer_report` is excluded on purpose: whether it is grounded depends on
        the claim it cites, which a single Claim cannot see. Following that
        chain is the caller's job -- see experiments/grounding.py.
        """
        return self.evidence_type in (EvidenceType.TEST_OUTPUT, EvidenceType.FILE_CONTENT)


class Spawn(_Strict):
    parent: str
    depth: int = Field(ge=0)
    max_depth: int = Field(ge=0)

    @field_validator("parent")
    @classmethod
    def _path(cls, v: str) -> str:
        if not AGENT_PATH.match(v):
            raise ValueError(f"malformed agent path: {v!r}")
        return v

    @model_validator(mode="after")
    def _within_max_depth(self) -> Spawn:
        if self.depth > self.max_depth:
            raise ValueError(f"depth {self.depth} exceeds max_depth {self.max_depth}")
        return self


class Budget(_Strict):
    """CONSERVED. A child can never hold more than its parent gave it.

    Conservation is what terminates recursion -- a branch that cannot afford one
    call cannot spawn, and answers spawn_refused. There is no separate fork-bomb
    guard because there does not need to be one. Slicing lives in P2's
    budget.py; this type only guarantees a budget is never negative.
    """

    usd_remaining: float = Field(ge=0)
    turns_remaining: int = Field(ge=0)


class ContextState(_Strict):
    """Advisory. A sender seeing used/limit > 0.9 switches to refs and summaries."""

    used: int = Field(ge=0)
    limit: int = Field(gt=0)

    @property
    def pressure(self) -> float:
        return self.used / self.limit


class Cost(_Strict):
    """Reporting only. Present on every task_result and child_result."""

    input: int = Field(ge=0)
    output: int = Field(ge=0)
    cache_read: int = Field(ge=0)
    usd: float = Field(ge=0)
    model: str


class Message(_Strict):
    """The envelope. Identical over inproc, loopback and lan."""

    protocol_version: str
    message_id: str
    conversation_id: str
    reply_to: str | None = None
    turn: int = Field(ge=0)
    lamport: int = Field(ge=0)
    timestamp: str

    sender: str
    receiver: str
    performative: Performative

    root_task: str
    payload: dict[str, Any] = Field(default_factory=dict)
    task_status: TaskStatus

    artifacts: list[ArtifactRef] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)

    spawn: Spawn
    budget: Budget
    context: ContextState
    cost: Cost | None = None

    sig: str | None = None

    # --- identity ---------------------------------------------------------

    @field_validator("protocol_version")
    @classmethod
    def _exact_version(cls, v: str) -> str:
        """Rejected, never coerced, never best-effort parsed.

        With four assistants in the repo, silent schema drift is the most likely
        way this project fails. A loud rejection is the point.
        """
        if v != PROTOCOL_VERSION:
            raise ValueError(f"protocol_version must be {PROTOCOL_VERSION!r}, got {v!r}")
        return v

    @field_validator("message_id", "reply_to")
    @classmethod
    def _uuid(cls, v: str | None) -> str | None:
        if v is not None and not _UUID.match(v):
            raise ValueError(f"must be a uuid4 string, got {v!r}")
        return v

    @field_validator("timestamp")
    @classmethod
    def _iso8601(cls, v: str) -> str:
        """Kept as the sender's exact string, not a parsed datetime.

        The signature covers the canonical JSON of this field. Re-serialising a
        datetime could hand back different bytes than arrived -- '+00:00' for
        'Z', or a dropped microsecond -- and every signature would fail for a
        reason nobody would find quickly. Validated, then left alone.
        """
        try:
            datetime.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(f"timestamp must be ISO 8601, got {v!r}") from exc
        return v

    # --- addressing -------------------------------------------------------

    @field_validator("sender", "receiver")
    @classmethod
    def _path(cls, v: str) -> str:
        if not AGENT_PATH.match(v):
            raise ValueError(f"malformed agent path: {v!r}")
        return v

    @model_validator(mode="after")
    def _spawn_agrees_with_sender(self) -> Message:
        """Depth in the path must equal spawn.depth, and parent must follow it.

        A mismatch is a routing bug in waiting: the address says one thing about
        where an agent sits in the tree and the spawn block says another, and
        whichever the receiver believes, the other is wrong.
        """
        actual = depth_of(self.sender)
        if self.spawn.depth != actual:
            raise ValueError(
                f"spawn.depth {self.spawn.depth} != depth of sender {self.sender!r} ({actual})"
            )
        expected_parent = parent_of(self.sender)
        if self.spawn.parent != expected_parent:
            raise ValueError(
                f"spawn.parent {self.spawn.parent!r} != sender minus last segment "
                f"({expected_parent!r})"
            )
        return self

    # --- content ----------------------------------------------------------

    @model_validator(mode="after")
    def _complete_requires_exit_zero(self) -> Message:
        """task_status: complete requires exit_code == 0 when one was reported.

        The single most effective hallucination control in the system, and it
        works only because it lives in a code path. The runtime is the arbiter;
        the model is advisory. P2 enforces this again in agent.py over the
        conversation's history -- this is the envelope-local half, which catches
        a message that contradicts itself outright.
        """
        exit_code = self.payload.get("exit_code")
        if self.task_status is TaskStatus.COMPLETE and exit_code not in (None, 0):
            raise ValueError(f"task_status 'complete' with exit_code {exit_code!r}")
        return self

    @model_validator(mode="after")
    def _refusal_carries_a_known_reason(self) -> Message:
        """spawn_refused is a protocol state peers reason about, so the reason
        has to be one of the four they know how to read."""
        if self.performative is Performative.SPAWN_REFUSED:
            reason = self.payload.get("reason")
            if reason not in SPAWN_REFUSED_REASONS:
                raise ValueError(
                    f"spawn_refused reason must be one of {sorted(SPAWN_REFUSED_REASONS)}, "
                    f"got {reason!r}"
                )
        return self

    # --- helpers ----------------------------------------------------------

    @property
    def peer(self) -> str:
        """The peer that owns the sender, whatever its depth."""
        return self.sender.split("/", 1)[0]

    @property
    def grounded_claims(self) -> int:
        return sum(1 for c in self.claims if c.grounded)
