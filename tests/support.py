"""Shared test helpers. Not a layer -- test infrastructure, owned by nobody.

Two jobs:

1. `implemented()` lets a contract test skip itself while the module it targets
   is still a stub, and activate the moment someone implements it. Nobody has to
   remember to delete a skip marker; the suite turns itself on.

2. `envelope()` builds a valid message as a plain dict, so the factory does not
   itself depend on protocol.py existing. Tests do `Message(**envelope())`.

Every field here is transcribed from docs/PROTOCOL.md. If this file and that
document disagree, this file is wrong.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

PROTOCOL_VERSION = "2.0"


def implemented(fn, *args: Any, **kwargs: Any) -> bool:
    """True once `fn` is no longer a `raise NotImplementedError` stub.

    The blind catch is deliberate and is the whole point of the probe: any
    exception other than NotImplementedError means the function ran and simply
    disliked these arguments, which still counts as implemented. Narrowing this
    would make the probe wrong.
    """
    import inspect

    try:
        result = fn(*args, **kwargs)
    except NotImplementedError:
        return False
    except Exception:  # noqa: BLE001 -- see docstring; anything else means "implemented"
        return True
    # Probing an async stub returns a coroutine that nobody awaits. Close it
    # explicitly or every probe emits a RuntimeWarning and the real signal
    # drowns in noise.
    if inspect.iscoroutine(result):
        result.close()
    return True


def has_message() -> bool:
    """True once protocol.py carries the envelope (the week-1 joint task)."""
    from samvad import protocol

    return hasattr(protocol, "Message")


def schema_error() -> type[BaseException]:
    """The exception a rejected envelope raises.

    Pydantic v2 per CLAUDE.md ("Pydantic v2 for anything crossing the wire").
    Asserting on this rather than bare `Exception` matters: a blind assert would
    also pass on a typo in the test itself, which is exactly the confident,
    incompatible reading these tests exist to catch.
    """
    from pydantic import ValidationError

    return ValidationError


def now_iso(offset_seconds: float = 0.0) -> str:
    """ISO 8601 UTC, optionally shifted -- for replay-window tests."""
    t = datetime.now(UTC).timestamp() + offset_seconds
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def envelope(**overrides: Any) -> dict[str, Any]:
    """A valid task_request envelope as a dict. Override any field by keyword.

    Deliberately complete: every required field from docs/PROTOCOL.md is
    present, so a test that wants to prove one field is rejected can override
    exactly that field and nothing else.
    """
    msg: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": str(uuid4()),
        "conversation_id": "conv-7c1e9a",
        "reply_to": None,
        "turn": 0,
        "lamport": 1,
        "timestamp": now_iso(),
        "sender": "agent_a",
        "receiver": "agent_b",
        "performative": "task_request",
        "root_task": "Implement a binary search function",
        "payload": {
            "subtask": "add unit tests for empty and single-element input",
            "constraints": ["O(log n)", "Python 3.11", "pytest"],
        },
        "artifacts": [],
        "claims": [],
        "spawn": {"parent": "agent_a", "depth": 0, "max_depth": 3},
        "budget": {"usd_remaining": 0.50, "turns_remaining": 12},
        "context": {"used": 14200, "limit": 200000},
        "cost": None,
        "task_status": "pending",
        "sig": None,
    }
    msg.update(overrides)
    return msg


def sha256_ref(payload: bytes = b"binary search impl") -> str:
    """A well-formed `sha256:` artifact ref, for tests that need a valid one."""
    import hashlib

    return "sha256:" + hashlib.sha256(payload).hexdigest()
