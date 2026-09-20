"""Node entry point. Wires the four layers together.

    MOCK_LLM=1 python -m samvad.node --config config/peers.yaml --as agent_a

SHARED FILE -- changes need all four owners to agree.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
import yaml

from samvad.agent import Agent
from samvad.clock import LamportClock
from samvad.events import BUS
from samvad.protocol import Message, Performative, TaskStatus, depth_of, parent_of
from samvad.server import make_app
from samvad.store.blobs import BlobStore
from samvad.store.log import MessageLog
from samvad.transport.lan import DeliveryFailed, DeliveryRefused, LanTransport

DEFAULT_PEERS: dict[str, dict[str, Any]] = {
    name: {"host": "127.0.0.1", "port": 8000 + i, "role": role, "model": "mock"}
    for i, (name, role) in enumerate(
        [("agent_a", "planner"), ("agent_b", "executor"),
         ("agent_c", "reviewer"), ("agent_d", "executor")]
    )
}
DEFAULT_BUDGET = {"usd": 0.50, "turns": 12}
DEFAULT_MAX_DEPTH = 3

#: How often each peer is probed. Fast enough that a killed laptop is visible
#: within the beat it is killed in; slow enough not to be its own traffic.
PEER_POLL_SECONDS = 2.0

#: Consecutive failed probes before a peer counts as dead. Campus wifi drops
#: packets, and reparenting a live peer's children is worse than reacting a few
#: seconds late -- so one missed probe is never enough.
PEER_DEATH_THRESHOLD = 3


def load_config(path: str | None) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Peer table, budget defaults and max_depth.

    Falls back to four peers on 127.0.0.1:8000-8003 when no config is given,
    so `python -m samvad.node --as agent_a` runs on one laptop with nothing
    else set up. peers.yaml is gitignored -- addresses differ per person and
    change per network -- so a working default matters more than usual here.
    """
    if not path:
        return DEFAULT_PEERS, DEFAULT_BUDGET, DEFAULT_MAX_DEPTH

    config = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    peers = config.get("peers") or DEFAULT_PEERS
    defaults = config.get("defaults") or {}
    return (
        peers,
        defaults.get("budget") or DEFAULT_BUDGET,
        int(defaults.get("max_depth", DEFAULT_MAX_DEPTH)),
    )


def new_message(
    sender: str,
    receiver: str,
    performative: Performative,
    root_task: str,
    *,
    lamport: int,
    conversation_id: str | None = None,
    payload: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Message:
    budget = budget or DEFAULT_BUDGET
    return Message(
        protocol_version="2.0",
        message_id=str(uuid4()),
        conversation_id=conversation_id or f"conv-{uuid4().hex[:8]}",
        turn=0,
        lamport=lamport,
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        sender=sender,
        receiver=receiver,
        performative=performative,
        root_task=root_task,
        payload=payload or {},
        task_status=TaskStatus.PENDING,
        spawn={"parent": parent_of(sender), "depth": depth_of(sender), "max_depth": max_depth},
        budget={"usd_remaining": float(budget["usd"]), "turns_remaining": int(budget["turns"])},
        context={"used": 0, "limit": 200_000},
    )


class Node:
    """One agent, its store, its transport, and the HTTP server around them."""

    def __init__(
        self, name: str, config: str | None = None, db: str | None = None,
        probe: Callable[[str, int], Awaitable[str]] | None = None,
    ) -> None:
        self.name = name
        self.peers, self.budget, self.max_depth = load_config(config)
        if name not in self.peers:
            raise SystemExit(f"{name!r} is not in the peer table: {sorted(self.peers)}")

        self.secret = os.environ.get("SAMVAD_SECRET", "")
        if not self.secret:
            raise SystemExit(
                "SAMVAD_SECRET is not set. It must be byte-identical on every device -- "
                'generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
            )

        self._check_port_free(int(self.peers[name]["port"]))

        self.clock = LamportClock()
        self.log = MessageLog(db or ":memory:")
        self.blobs = BlobStore(db or ":memory:")
        self.model = self.peers[name].get("model", "mock")
        self.agent = Agent(agent_path=name, clock=self.clock, model=self.model)
        self.transport = LanTransport(self.peers, secret=self.secret)

        # Peer liveness. Injectable so the failover contract test can drive
        # death without unplugging anything.
        from samvad import config as peer_config

        self._probe = probe or peer_config.probe
        self._peer_failures: dict[str, int] = {}
        self._peer_down: set[str] = set()
        self._reparented: set[str] = set()

        self.app = make_app(
            self.inbox,
            agent=name,
            peers=tuple(self.peers),
            secret=self.secret,
            log=self.log,
            blobs=self.blobs,
        )

    def _check_port_free(self, port: int) -> None:
        """Fail before the event loop starts, not inside it.

        "Port already in use" is the most common failure when bringing four
        nodes up, usually a previous run that did not exit. Raised from
        __init__ it is one clean line; raised inside serve() it surfaces as an
        unretrieved-task traceback with the real message buried at the bottom.
        """
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            raise SystemExit(
                chr(10).join([
                    f"[{self.name}] port {port} is already in use",
                    "  another node is probably still running.",
                    f"  Windows:   netstat -ano | findstr :{port}",
                    f"  mac/Linux: lsof -i :{port}",
                ])
            ) from None
        finally:
            probe.close()

    async def inbox(self, msg: Message) -> None:
        """A message arrived. Think, then send whatever came back.

        This is the only place the two halves meet: agent.handle() RETURNS
        messages and never sends them, and the transport sends whatever it is
        handed. Keeping the join here is what lets the agent core be tested
        with no network and the transport be tested with no model.

        Nothing raises out of this function. It runs as a background task
        scheduled by POST /message, and an exception here would be swallowed by
        the event loop and lost -- so a delivery failure is logged and the node
        stays up. On a real LAN peers do go away; that is the condition the
        protocol exists to survive, not a crash.
        """
        BUS.publish_message(msg)
        print(f'[{self.name}] <- {msg.sender:<9} {msg.performative!s:<15} '
              f'lamport={msg.lamport:<3} {msg.task_status}')
        try:
            replies = await self.agent.handle(msg)
        except Exception as exc:  # noqa: BLE001 -- see docstring: never kill the node
            print(f"[{self.name}] handler error on {msg.message_id}: {exc!r}", file=sys.stderr)
            return

        for reply in replies:
            try:
                await self.transport.send(reply)
            except DeliveryRefused as exc:
                print(f"[{self.name}] refused: {exc}", file=sys.stderr)
            except DeliveryFailed as exc:
                # The loss measurement 1 exists to count. In-process this cannot
                # happen; on a LAN it does, which is the whole argument.
                print(f"[{self.name}] undelivered: {exc}", file=sys.stderr)
            else:
                BUS.publish_message(reply)
                self.log.append(reply)
                self.log.record_cost(reply)
                print(f'[{self.name}] -> {reply.receiver:<9} {reply.performative!s:<15} '
                      f'lamport={reply.lamport:<3} {reply.task_status} '
                      f'turns_left={reply.budget.turns_remaining}')

        self.publish_state()

    # --- surviving a peer that dies ---------------------------------------

    def peers_to_watch(self) -> tuple[str, ...]:
        """Every peer but this one. A node probing itself proves nothing."""
        return tuple(p for p in self.peers if p != self.name)

    def observe_peer(self, peer: str, status: str) -> bool:
        """Record one probe result. True only on the transition into dead.

        Returning True exactly once per death is what makes the watcher safe to
        run on a timer: reparenting is triggered by the edge, not by the state,
        so a peer that stays down does not re-trigger it every two seconds.
        """
        if status == "reachable":
            self._peer_failures.pop(peer, None)
            if peer in self._peer_down:
                self._peer_down.discard(peer)
                BUS.publish_node(peer, up=True)
                print(f"[{self.name}] {peer} is back", file=sys.stderr)
            return False

        if peer in self._peer_down:
            return False

        failures = self._peer_failures.get(peer, 0) + 1
        self._peer_failures[peer] = failures
        if failures < PEER_DEATH_THRESHOLD:
            return False

        self._peer_down.add(peer)
        # Published here rather than in on_peer_down: a peer with no children
        # to reparent is still a peer that went away, and the dashboard has to
        # grey it out either way. That strip is the kill-a-peer beat's visual.
        BUS.publish_node(peer, up=False)
        return True

    def on_peer_down(self, peer: str) -> list[str]:
        """Reparent the dead peer's unfinished children to this node.

        docs/DEMO.md: "Orphans reparent to the grandparent. In-flight results
        still arrive. The task completes." All three fall out of this:

        * `orphans_of` finds the children this node learned about from their
          spawn_acks, skipping any that already delivered.
        * `reparent` drops the dead parent's ownership record, `adopt` takes it
          up here -- carrying the group id and budget slice across, because the
          slice is what bounds the orphan's own recursion.
        * Paths are NOT rewritten. Routing is by address prefix, so a result
          already in flight from `agent_b/worker_1` still lands here, and
          rewriting the path would invalidate its signature anyway.

        Idempotent: a path already moved is never moved twice.
        """
        supervisor = self.agent.supervisor
        orphans = [p for p in supervisor.orphans_of(peer) if p not in self._reparented]
        if not orphans:
            return []

        records = {p: supervisor.child_record(p) for p in orphans}
        moved = supervisor.reparent(orphans, to=self.name)
        for path in moved:
            record = records[path]
            if record is not None:
                supervisor.adopt(path, record.group_id, record.budget_slice)
        self._reparented.update(moved)

        print(f"[{self.name}] {peer} is down -- reparented {len(moved)} orphan(s) "
              f"to {self.name}: {', '.join(moved)}", file=sys.stderr)
        self.publish_state()
        return moved

    async def _watch_peers(self) -> None:
        """Probe every peer on a timer; reparent on the transition into dead.

        Never raises: this runs for the life of the node, and a watcher that
        dies on one bad probe would take the demo's most important behaviour
        with it, silently.
        """
        while True:
            await asyncio.sleep(PEER_POLL_SECONDS)
            for peer in self.peers_to_watch():
                cfg = self.peers[peer]
                try:
                    status = await self._probe(str(cfg["host"]), int(cfg["port"]))
                except Exception:  # noqa: BLE001 -- see docstring
                    status = "timeout"
                if self.observe_peer(peer, status):
                    self.on_peer_down(peer)

    # --- surviving a restart ----------------------------------------------

    def restore(self) -> int:
        """Replay the log and resume the Lamport clock. Returns messages seen.

        A node that restarts at zero reorders its own history: every message it
        sends afterwards looks older than the ones already on disk, and the
        append-only log stops being orderable. Replay is in (lamport, sender)
        order, never timestamp, for the same reason everything else here is.

        With the default in-memory store there is nothing on disk to replay --
        pass `--db` for a node that should survive being restarted.
        """
        replayed = 0
        for msg in self.log.replay():
            self.clock.observe(msg.lamport)
            replayed += 1
        if replayed:
            print(f"[{self.name}] replayed {replayed} messages; "
                  f"lamport resumes at {self.clock.value}")
        return replayed

    def publish_state(self) -> None:
        # GET /health reads app.state.children; the supervisor is the only thing
        # that actually knows. Without this the dashboard and /health disagree
        # about the same node -- and demo beat 1 puts /health on the projector.
        self.app.state.children = self.agent.supervisor.active_count
        BUS.publish_node(
            self.name,
            model=self.model,
            role=self.peers[self.name].get("role", ""),
            up=True,
            context={"used": self.agent.context.used(), "limit": self.agent.context.limit()},
            children=self.agent.supervisor.active_count,
            budget_usd=max(0.0, float(self.budget["usd"]) - self.log.total_usd()),
        )

    async def serve(self) -> None:
        cfg = self.peers[self.name]
        port = int(cfg["port"])


        server = uvicorn.Server(
            uvicorn.Config(self.app, host="0.0.0.0", port=port, log_level="warning")
        )
        self.publish_state()
        print(f"[{self.name}] listening on {cfg['host']}:{cfg['port']}  "
              f"role={cfg.get('role', '?')}  model={self.model}")

        watcher = asyncio.create_task(self._watch_peers())
        try:
            await server.serve()
        finally:
            watcher.cancel()

    async def kick_off(self, task: str, to: str) -> None:
        """Send the first task_request, opening a conversation."""
        msg = new_message(
            self.name, to, Performative.TASK_REQUEST, task,
            lamport=self.clock.tick(), budget=self.budget, max_depth=self.max_depth,
            payload={"subtask": task, "constraints": []},
        )
        await self.transport.send(msg)
        self.log.append(msg)
        BUS.publish_message(msg)
        print(f'[{self.name}] -> {to:<9} task_request   lamport={msg.lamport:<3} {task!r}')


async def run(args: argparse.Namespace) -> None:
    node = Node(args.as_agent, config=args.config, db=args.db)
    node.restore()
    serving = asyncio.create_task(node.serve())
    if args.task:
        await asyncio.sleep(args.delay)
        await node.kick_off(args.task, args.to)
    await serving


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m samvad.node", description="Run one Samvad agent."
    )
    parser.add_argument("--as", dest="as_agent", required=True, help="which peer this device is")
    parser.add_argument("--config", help="path to peers.yaml (default: four peers on localhost)")
    parser.add_argument("--db", help="sqlite path. WITHOUT this the log is in-memory and a "
                                     "restart replays nothing -- pass it for any run where a "
                                     "node is meant to survive being killed")
    parser.add_argument("--task", help="send this task on startup, opening a conversation")
    parser.add_argument("--to", default="agent_b", help="who --task is addressed to")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds to wait before --task, so peers can come up")
    args = parser.parse_args()

    if os.environ.get("MOCK_LLM", "1") == "0":
        print("WARNING: MOCK_LLM=0 -- this node will make real, billable API calls.",
              file=sys.stderr)

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print(f"\n[{args.as_agent}] stopped")


if __name__ == "__main__":
    main()
