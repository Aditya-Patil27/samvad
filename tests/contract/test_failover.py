"""Contract: surviving a peer that dies, and a node that restarts.

Written before the wiring, per CLAUDE.md.

docs/DEMO.md calls the kill-a-peer beat "the money shot" and it is the one
claim this system can make that an in-process one structurally cannot: there is
nothing to kill. Until now `Supervisor.reparent()`, `adopt()` and `orphans_of()`
were implemented and unit-tested but nothing called them, and `MessageLog.replay()`
was never called on boot -- so both beats were true of the library and false of
the running system.

What these tests defend:

1. **A peer's children are known before it dies.** A grandparent cannot adopt an
   orphan it never heard of. `spawn_ack` already carries the child's path in
   `payload` -- no new field, per CLAUDE.md rule 3 -- and the requester has to
   record it at the moment it arrives, because after the peer is gone there is
   nobody left to ask.
2. **Death is decided by repetition, not by one failed probe.** A single dropped
   packet on campus wifi is not a dead laptop. Reparenting a live peer's children
   is worse than reacting slowly.
3. **Budget survives the move.** The child's slice is what bounds its recursion.
   An orphan adopted without one could spawn freely, and budget conservation is
   the only thing terminating this system.
4. **A restarted node resumes its clock.** Lamport ordering across a restart is
   what makes the append-only log worth keeping; a node that restarts at zero
   reorders its own history.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from tests.support import envelope, has_message, implemented

pytestmark = pytest.mark.skipif(
    not has_message(),
    reason="week 1 joint task: write protocol.py together first",
)


def _msg(**overrides):
    from samvad.protocol import Message

    return Message(**envelope(**overrides))


def _peers_yaml(tmp_path: Path, ports: dict[str, int]) -> str:
    path = tmp_path / "peers.yaml"
    path.write_text(yaml.safe_dump({
        "peers": {
            name: {"host": "127.0.0.1", "port": port, "role": "executor", "model": "mock"}
            for name, port in ports.items()
        },
        "defaults": {"max_depth": 3, "budget": {"usd": 0.50, "turns": 12}},
    }), encoding="utf-8")
    return str(path)


def _free_ports(n: int) -> list[int]:
    import socket

    socks = []
    try:
        for _ in range(n):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            socks.append(s)
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def _node(tmp_path: Path, name: str = "agent_a", db: str | None = None, **kw):
    from samvad.node import Node

    names = ["agent_a", "agent_b", "agent_c", "agent_d"]
    ports = dict(zip(names, _free_ports(4)))
    return Node(name, config=_peers_yaml(tmp_path, ports), db=db, **kw)


# ==========================================================================
# a grandparent learns its grandchildren from spawn_ack
# ==========================================================================

def test_spawn_ack_registers_the_remote_child_with_the_supervisor():
    """The only moment the requester is told a remote child exists.

    `_handle_spawn` already answers a spawn_request with one spawn_ack per
    child, carrying `payload['agent_path']`. Dropping it on the floor is what
    left the grandparent unable to adopt anything later.
    """
    from samvad.agent import Agent

    agent = Agent(agent_path="agent_a")
    ack = _msg(sender="agent_b", receiver="agent_a", performative="spawn_ack",
               reply_to=None, payload={"agent_path": "agent_b/worker_1"})

    assert implemented(agent.handle, ack), "agent.handle not implemented yet"
    asyncio.run(agent.handle(ack))

    assert agent.supervisor.child_record("agent_b/worker_1") is not None


def test_a_spawn_ack_without_an_agent_path_is_ignored_not_crashed():
    """Validate at boundaries. A malformed ack from a peer must not take the
    node down -- it is exactly the kind of thing a half-written peer sends."""
    from samvad.agent import Agent

    agent = Agent(agent_path="agent_a")
    ack = _msg(sender="agent_b", receiver="agent_a", performative="spawn_ack", payload={})
    asyncio.run(agent.handle(ack))
    assert agent.supervisor.active_count == 0


def test_orphans_of_finds_exactly_the_dead_peers_children():
    from samvad.agent import Agent

    agent = Agent(agent_path="agent_a")
    for peer, worker in (("agent_b", 1), ("agent_b", 2), ("agent_c", 1)):
        asyncio.run(agent.handle(_msg(
            sender=peer, receiver="agent_a", performative="spawn_ack",
            payload={"agent_path": f"{peer}/worker_{worker}"})))

    assert sorted(agent.supervisor.orphans_of("agent_b")) == [
        "agent_b/worker_1", "agent_b/worker_2"]
    assert agent.supervisor.orphans_of("agent_c") == ["agent_c/worker_1"]


# ==========================================================================
# the kill-a-peer beat
# ==========================================================================

def test_peer_death_reparents_the_orphans_to_this_node(tmp_path):
    node = _node(tmp_path)
    for worker in (1, 2, 3):
        asyncio.run(node.agent.handle(_msg(
            sender="agent_b", receiver="agent_a", performative="spawn_ack",
            payload={"agent_path": f"agent_b/worker_{worker}"})))

    reparented = node.on_peer_down("agent_b")

    assert sorted(reparented) == ["agent_b/worker_1", "agent_b/worker_2", "agent_b/worker_3"]
    for path in reparented:
        assert node.agent.supervisor.child_record(path) is not None, \
            "an orphan must still be tracked after the move, or its result is unclaimed"


def test_reparenting_preserves_the_budget_slice_that_bounds_the_orphan(tmp_path):
    """Budget conservation is the termination proof. An orphan adopted without
    its slice is an orphan that can spawn freely."""
    node = _node(tmp_path)
    asyncio.run(node.agent.handle(_msg(
        sender="agent_b", receiver="agent_a", performative="spawn_ack",
        payload={"agent_path": "agent_b/worker_1"},
        budget={"usd_remaining": 0.125, "turns_remaining": 3})))

    before = node.agent.supervisor.child_record("agent_b/worker_1")
    assert before is not None
    slice_before, group_before = before.budget_slice, before.group_id

    node.on_peer_down("agent_b")

    after = node.agent.supervisor.child_record("agent_b/worker_1")
    assert after is not None
    assert after.budget_slice.usd_remaining == slice_before.usd_remaining
    assert after.budget_slice.turns_remaining == slice_before.turns_remaining
    assert after.group_id == group_before, "a reparented orphan stays in its fan-out"


def test_a_finished_child_is_not_reparented(tmp_path):
    """It already delivered. Re-adopting it would leave a fan-out waiting on a
    result that arrived before the peer died."""
    node = _node(tmp_path)
    asyncio.run(node.agent.handle(_msg(
        sender="agent_b", receiver="agent_a", performative="spawn_ack",
        payload={"agent_path": "agent_b/worker_1"})))
    asyncio.run(node.agent.handle(_msg(
        sender="agent_b/worker_1", receiver="agent_a", performative="child_result",
        task_status="complete", payload={"result": "done", "exit_code": 0})))

    assert node.on_peer_down("agent_b") == []


def test_a_result_from_a_reparented_orphan_still_arrives(tmp_path):
    """docs/DEMO.md: 'Orphans reparent to the grandparent. In-flight results
    still arrive. The task completes.'"""
    node = _node(tmp_path)
    asyncio.run(node.agent.handle(_msg(
        sender="agent_b", receiver="agent_a", performative="spawn_ack",
        payload={"agent_path": "agent_b/worker_1"})))
    node.on_peer_down("agent_b")

    asyncio.run(node.agent.handle(_msg(
        sender="agent_b/worker_1", receiver="agent_a", performative="child_result",
        task_status="complete", payload={"result": "bisect_left", "exit_code": 0})))

    record = node.agent.supervisor.child_record("agent_b/worker_1")
    assert record is not None and record.result is not None


def test_peer_death_is_idempotent(tmp_path):
    """The watcher will call this on every poll while a peer stays down."""
    node = _node(tmp_path)
    asyncio.run(node.agent.handle(_msg(
        sender="agent_b", receiver="agent_a", performative="spawn_ack",
        payload={"agent_path": "agent_b/worker_1"})))

    assert node.on_peer_down("agent_b") == ["agent_b/worker_1"]
    assert node.on_peer_down("agent_b") == [], "a second death must not re-move anything"


# ==========================================================================
# death is decided by repetition
# ==========================================================================

def test_one_failed_probe_is_not_a_dead_peer(tmp_path):
    """Campus wifi drops packets. Reparenting a live peer's children is worse
    than reacting a few seconds late."""
    from samvad.node import PEER_DEATH_THRESHOLD

    node = _node(tmp_path)
    assert PEER_DEATH_THRESHOLD >= 2, "a single dropped probe must not kill a peer"

    for _ in range(PEER_DEATH_THRESHOLD - 1):
        assert node.observe_peer("agent_b", "timeout") is False
    assert node.observe_peer("agent_b", "timeout") is True, "threshold reached"


def test_a_reachable_probe_clears_the_failure_count(tmp_path):
    from samvad.node import PEER_DEATH_THRESHOLD

    node = _node(tmp_path)
    for _ in range(PEER_DEATH_THRESHOLD - 1):
        node.observe_peer("agent_b", "timeout")
    node.observe_peer("agent_b", "reachable")
    for _ in range(PEER_DEATH_THRESHOLD - 1):
        assert node.observe_peer("agent_b", "timeout") is False,             "a successful probe must reset the count, not decrement it"


def test_a_peer_is_reported_down_only_once_until_it_returns(tmp_path):
    from samvad.node import PEER_DEATH_THRESHOLD

    node = _node(tmp_path)
    for _ in range(PEER_DEATH_THRESHOLD):
        node.observe_peer("agent_b", "timeout")
    assert node.observe_peer("agent_b", "timeout") is False, "already down"

    node.observe_peer("agent_b", "reachable")
    for _ in range(PEER_DEATH_THRESHOLD - 1):
        node.observe_peer("agent_b", "timeout")
    assert node.observe_peer("agent_b", "timeout") is True, "it died again"


def test_the_node_never_probes_itself(tmp_path):
    node = _node(tmp_path, name="agent_a")
    assert "agent_a" not in node.peers_to_watch()
    assert set(node.peers_to_watch()) == {"agent_b", "agent_c", "agent_d"}


# ==========================================================================
# partition and heal: a restart resumes the clock
# ==========================================================================

def test_a_restarted_node_replays_its_log_and_resumes_its_lamport_clock(tmp_path):
    """Order by Lamport, never by timestamp -- across a restart too."""
    from samvad.store.log import MessageLog

    db = str(tmp_path / "agent_a.db")
    log = MessageLog(db)
    for lamport in (1, 2, 7):
        log.append(_msg(lamport=lamport))
    log.close()

    node = _node(tmp_path, db=db)
    replayed = node.restore()

    assert replayed == 3
    assert node.clock.tick() > 7, "the clock resumed below the log's high-water mark"


def test_a_node_with_no_log_replays_nothing_and_still_starts(tmp_path):
    node = _node(tmp_path)
    assert node.restore() == 0
    assert node.clock.tick() == 1


def test_replay_is_ordered_by_lamport_not_by_arrival(tmp_path):
    from samvad.store.log import MessageLog

    db = str(tmp_path / "agent_a.db")
    log = MessageLog(db)
    for lamport in (9, 2, 5):
        log.append(_msg(lamport=lamport))
    log.close()

    node = _node(tmp_path, db=db)
    node.restore()
    assert node.clock.tick() > 9
