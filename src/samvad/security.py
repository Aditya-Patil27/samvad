# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""HMAC signing and verification.

Signed on EVERY transport, inproc included. An unsigned fast path would be a
second code path and would invalidate measurement 1.

Digest covers canonical JSON of every field except `sig`: keys sorted,
no whitespace. Rejects bad digest (401), timestamp outside +/-120s (401,
replay), unknown sender (403).
"""

REPLAY_WINDOW_SECONDS = 120


def sign(msg, secret: str) -> str:
    """-> 'hmac-sha256:<hex>'"""
    raise NotImplementedError


def verify(msg, secret: str) -> bool:
    raise NotImplementedError


def canonical(msg) -> bytes:
    """Deterministic bytes for signing. Sorted keys, no whitespace, no `sig`."""
    raise NotImplementedError
