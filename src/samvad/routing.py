# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""Longest-prefix address resolution -- deliberately the same logic as IP routing.

    agent_b                      a peer          -> agent_b
    agent_b/worker_2             a child         -> agent_b, which forwards
    agent_b/worker_2/checker_1   a grandchild    -> agent_b, which forwards

Only peers appear in the peer table. Children never do.
"""


def resolve(address: str, table: dict):
    """Longest-prefix match against the peer table. Raises on no match."""
    raise NotImplementedError


def peer_prefix(address: str) -> str:
    """'agent_b/worker_2/checker_1' -> 'agent_b'"""
    raise NotImplementedError
