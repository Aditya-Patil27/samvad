"""Contract: address resolution. Owner P1.

Deliberately NOT gated on protocol.py. Routing is string handling against the
peer table -- it never touches the envelope, so it can be built and proved
before the week-1 protocol session.

Only peers appear in the peer table. Children never do: a message to
`agent_b/worker_2` routes to `agent_b`, which forwards.
"""
import pytest

from samvad.routing import peer_prefix, resolve

TABLE = {
    "agent_a": {"host": "192.168.1.41", "port": 8000, "role": "planner"},
    "agent_b": {"host": "192.168.1.42", "port": 8000, "role": "executor"},
    "agent_c": {"host": "192.168.1.43", "port": 8000, "role": "reviewer"},
}


def test_longest_prefix_routes_child_to_its_peer():
    """Every depth resolves to the same peer -- that peer forwards onward."""
    for address in ("agent_b", "agent_b/worker_2", "agent_b/worker_2/checker_1"):
        assert resolve(address, TABLE) == TABLE["agent_b"]


def test_resolution_picks_the_longest_match_not_the_first():
    """Deliberately the same logic as IP routing: a more specific entry wins.

    The peer table normally holds only peers, but the matcher must not depend
    on that -- if a child is ever registered directly, it takes precedence.
    """
    table = {**TABLE, "agent_b/worker_2": {"host": "192.168.1.99", "port": 8100}}
    assert resolve("agent_b/worker_2", table) == table["agent_b/worker_2"]
    assert resolve("agent_b/worker_2/checker_1", table) == table["agent_b/worker_2"]
    assert resolve("agent_b/worker_7", table) == TABLE["agent_b"]


def test_peer_prefix_strips_child_segments():
    assert peer_prefix("agent_b/worker_2/checker_1") == "agent_b"
    assert peer_prefix("agent_b/worker_2") == "agent_b"
    assert peer_prefix("agent_b") == "agent_b"


def test_unroutable_address_raises_rather_than_defaulting():
    """No match is an error. Silently falling back to some other peer would
    deliver a task to an agent that was never asked for it."""
    with pytest.raises(LookupError):
        resolve("agent_q/worker_1", TABLE)
    with pytest.raises(LookupError):
        resolve("agent_q", TABLE)


def test_a_partial_segment_is_not_a_match():
    """'agent_bb' must not resolve to 'agent_b'. Prefix means whole segments."""
    with pytest.raises(LookupError):
        resolve("agent_bb", TABLE)
    with pytest.raises(LookupError):
        resolve("agent_bb/worker_1", TABLE)


def test_malformed_paths_are_rejected():
    """Paths are lowercase [a-z0-9_] per segment, '/'-separated.

    resolve() reads an address off the wire, so it validates at the boundary
    rather than trusting it.
    """
    for bad in ("", "/", "agent_b/", "/agent_b", "agent_b//worker_2", "Agent_B", "agent b"):
        with pytest.raises(ValueError):
            resolve(bad, TABLE)


def test_peer_prefix_rejects_malformed_paths_too():
    for bad in ("", "agent_b/", "AGENT_B"):
        with pytest.raises(ValueError):
            peer_prefix(bad)


def test_an_empty_table_routes_nothing():
    with pytest.raises(LookupError):
        resolve("agent_a", {})
