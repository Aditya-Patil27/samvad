# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""HMAC signing and verification.

Signed on EVERY transport, inproc included. An unsigned fast path would be a
second code path and would invalidate measurement 1.

Digest covers canonical JSON of every field except `sig`: keys sorted,
no whitespace. Rejects bad digest (401), timestamp outside +/-120s (401,
replay), unknown sender (403).
"""
from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

REPLAY_WINDOW_SECONDS = 120

PREFIX = "hmac-sha256:"


def canonical(msg: Any) -> bytes:
    """Deterministic bytes for signing. Sorted keys, no whitespace, no `sig`.

    Both sides must agree on these bytes exactly or every signature fails, so
    the three things that could differ are all pinned:

      sorted keys      -- dict order is insertion order in Python, and two
                          agents building the same message in a different order
                          would otherwise produce different bytes
      no whitespace    -- the default `json.dumps` separators include spaces
      `sig` excluded   -- signing a field that holds the signature is circular

    `mode="json"` renders through the same serialiser the wire uses, so what is
    signed is byte-for-byte what is sent.
    """
    body = msg.model_dump(mode="json") if hasattr(msg, "model_dump") else dict(msg)
    body.pop("sig", None)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(msg: Any, secret: str) -> str:
    """-> 'hmac-sha256:<hex>'"""
    digest = hmac.new(secret.encode("utf-8"), canonical(msg), sha256).hexdigest()
    return PREFIX + digest


def within_replay_window(timestamp: str, now: datetime | None = None) -> bool:
    """True if `timestamp` is within +/-120s of now.

    Symmetric on purpose: four laptops have four wall clocks, and one running
    slightly fast would otherwise have every message it sends rejected as being
    from the future. This is the only place wall time is used for a decision --
    ordering is Lamport, always.
    """
    try:
        sent = datetime.fromisoformat(timestamp)
    except (ValueError, AttributeError):
        return False
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return abs((reference - sent).total_seconds()) <= REPLAY_WINDOW_SECONDS


def verify(msg: Any, secret: str, now: datetime | None = None) -> bool:
    """Digest and replay window. False, never an exception.

    Returns a bool rather than raising because the caller is an HTTP handler
    deciding a status code, not a code path recovering from an error. The
    unknown-sender check (403) needs the peer table and lives in server.py.
    """
    sig = getattr(msg, "sig", None) if not isinstance(msg, dict) else msg.get("sig")
    if not isinstance(sig, str) or not sig.startswith(PREFIX):
        return False

    expected = sign(msg, secret)
    # compare_digest, not ==: a plain comparison returns early on the first
    # differing byte, and the timing leaks how much of the digest was right.
    if not hmac.compare_digest(sig, expected):
        return False

    timestamp = getattr(msg, "timestamp", None) if not isinstance(msg, dict) else msg.get("timestamp")
    return within_replay_window(timestamp, now)
