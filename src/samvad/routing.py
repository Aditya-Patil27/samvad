# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""Longest-prefix address resolution -- deliberately the same logic as IP routing.

    agent_b                      a peer          -> agent_b
    agent_b/worker_2             a child         -> agent_b, which forwards
    agent_b/worker_2/checker_1   a grandchild    -> agent_b, which forwards

Only peers appear in the peer table. Children never do.
"""
import re

# Agent paths: '/'-separated, lowercase, [a-z0-9_] per segment.
# See docs/PROTOCOL.md -- addressing.
_PATH = re.compile(r"^[a-z0-9_]+(?:/[a-z0-9_]+)*$")


def _segments(address: str) -> list[str]:
    """Validate an agent path and split it. Raises ValueError if malformed.

    Addresses arrive off the wire, so they are checked at this boundary rather
    than trusted. An unvalidated path would let '' or 'agent_b/' match by
    accident and deliver a task to the wrong agent.
    """
    if not isinstance(address, str) or not _PATH.match(address):
        raise ValueError(f"malformed agent path: {address!r}")
    return address.split("/")


def resolve(address: str, table: dict):
    """Longest-prefix match against the peer table. Raises on no match.

    Walks from the most specific prefix to the least, so a directly registered
    child wins over its peer -- the same precedence IP routing gives a more
    specific route. In normal operation the table holds only peers and this
    settles on the first segment, but the matcher does not depend on that.

    Raises:
        ValueError: the address is not a well-formed agent path.
        LookupError: no prefix of the address is in the table. Never falls back
            to a default peer -- that would deliver a task to an agent nobody
            addressed.
    """
    segments = _segments(address)
    for end in range(len(segments), 0, -1):
        candidate = "/".join(segments[:end])
        if candidate in table:
            return table[candidate]
    raise LookupError(f"no route to {address!r} in peer table {sorted(table)}")


def peer_prefix(address: str) -> str:
    """'agent_b/worker_2/checker_1' -> 'agent_b'

    The peer that owns an address, whatever its depth. This is who a message
    is physically sent to; that peer forwards to the child itself.
    """
    return _segments(address)[0]
