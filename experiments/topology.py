# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Measurement 2: star vs ring vs mesh -- O(N) against O(N^2)

Method: docs/EXPERIMENTS.md#2

    python -m experiments.topology --nodes 4 --shape star,ring,mesh
    python -m experiments.topology --nodes 4 --transport inproc,loopback

**The counts are measured, not computed.** Every message is a real envelope,
signed, handed to a real transport and verified by its receiver; only one that
arrived and verified counts. The closed form is printed beside the measurement
so the two can disagree in public, and is never written into a row. **No LLM is
called**: each result is a placeholder and `run_models` is `none-transport-only`.

**One completion condition for every shape.** The planner (`agent_a`) poses the
task and each of the N peers contributes one result. The task is complete when
*every* peer holds *all N results* -- an all-gather, the state a review panel
needs before it can vote. One rule for all shapes: a peer sends a message only
if it carries a result the receiver lacks.

* `star`: A sends `task_request`, with its own result, to each worker; each
  replies `task_result` with its own; once A holds all N it sends each worker
  what it lacks (nothing at N=2). 3(N-1) messages over 3 hops; 2/2 at N=2.
* `ring`: each peer to its successor `(i+1) % N`, the token carrying every
  result its sender holds. The first peer to hold all N names itself
  `first_complete`; a peer forwards unless its successor is `first_complete`.
  2(N-1) messages over 2(N-1) hops: O(N), but every one on the critical path.
* `mesh`: A sends `task_request` to each worker; each worker sends its result
  as `task_result` to every other peer. N(N-1) messages over 2 hops.

    N   star   ring   mesh    (messages / hops)
    2   2/2    2/2    2/2     one link, no routing choice: if these differ,
    3   6/3    4/4    6/2     the harness is measuring itself
    4   9/3    6/6    12/2

**Hops** are causal depth along `reply_to`: a planner message is hop 1, a reply
one more than what it answers, and a task's `hops` its longest chain. `turn`
follows the same chain; Lamport stamps come from one clock per peer; `seq` is
arrival order in this process and makes no ordering claim.

Per task: messages delivered and sent, hops, signed bytes, and wall clock from
the planner's first send to the delivery that completes the all-gather. The
summary takes median and p95 from experiments/harness.py per (transport, shape,
N); hops and wall clock cover completed tasks only. `messages_per_completed_task`
charges every message sent, finished or not, to the tasks that completed.

**Three N points show the shape of a curve. They are not enough to fit one.**
N = 2, 3, 4 is all four permanent peers allow. Read the summary as "mesh grows
faster than star and ring", never as an exponent.

**Nothing here crossed a LAN.** `inproc` is one process; `loopback` runs real
uvicorn servers on 127.0.0.1 that verify, dedupe and answer 202 as a node does.
There is no `lan` condition: the reacting peers are this harness's handlers, and
`samvad.node` has no topology rules to run. Every row says `run_network=one-host`
and the CLI refuses any other label. So the doc's caveat goes untested: mesh wins
on hops here because one host has no per-link variance; on four laptops it may not.

Writes `topology_messages.csv`, `topology_tasks.csv` and `topology_summary.csv`
to `results/`; every row carries the `run_*` metadata, `run_node_count` its own N.
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx

from experiments import harness, transport
from samvad import security
from samvad.clock import LamportClock
from samvad.protocol import PROTOCOL_VERSION, Message, Performative
from samvad.server import make_app
from samvad.transport.inproc import InProcTransport
from samvad.transport.lan import DeliveryFailed, DeliveryRefused, LanTransport
from samvad.transport.loopback import LoopbackTransport

PEERS = transport.PEERS
SHAPES: tuple[str, ...] = ("star", "ring", "mesh")
WIRES: tuple[str, ...] = ("inproc", "loopback")
MIN_NODES, MAX_NODES = 2, len(PEERS)

#: Clear of 8000-8003 (nodes), 8101-8103 (demo) and 8200-8203 (measurement 1).
DEFAULT_BASE_PORT = 8210

_REQUEST, _RESULT = Performative.TASK_REQUEST, Performative.TASK_RESULT
_WORDS = {1: "one", 2: "two", 3: "three"}


def theory(shape: str, n: int) -> tuple[int, int]:
    """(messages, hops) for one task: the table in the module docstring."""
    _check(shapes=(shape,), nodes=(n,))
    if shape == "star":
        return (3 * (n - 1), 3) if n > 2 else (2, 2)
    if shape == "ring":
        return 2 * (n - 1), 2 * (n - 1)
    return n * (n - 1), 2


def _check(*, shapes: Sequence[str] = (), nodes: Sequence[int] = (),
           wires: Sequence[str] = ()) -> None:
    for kind, values, allowed in (("shape", shapes, SHAPES), ("transport", wires, WIRES)):
        for value in values:
            if value not in allowed:
                raise ValueError(f"unknown {kind}: {value!r}; expected one of {allowed}")
    for n in nodes:
        if not MIN_NODES <= n <= MAX_NODES:
            raise ValueError(f"nodes must be {MIN_NODES}..{MAX_NODES}, got {n}")


# --- one task --------------------------------------------------------------

@dataclass
class TaskRun:
    """What one task cost. `delivered` holds (envelope, ms after start) in
    arrival order; `wall_ms` stays None unless the all-gather completed."""

    wire: str
    shape: str
    n: int
    task_id: str
    conversation_id: str
    sent: int = 0
    delivered: list[tuple[Message, float]] = field(default_factory=list)
    wall_ms: float | None = None


@dataclass
class _Live:
    run: TaskRun
    peers: tuple[str, ...]
    task: Mapping[str, Any]
    held: dict[str, set[str]]
    clocks: dict[str, LamportClock]
    started: float = 0.0
    hub_done: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: BaseException | None = None

    def has_all(self, peer: str) -> bool:
        return len(self.held[peer]) == len(self.peers)


class Network:
    """N peers on one routing rule over one real transport. `send` is an attribute so a
    test can swap the wire; `inbox` verifies the signature before anything counts."""

    def __init__(self, *, secret: str, wire: str = "inproc",
                 http: LanTransport | None = None) -> None:
        _check(wires=(wire,))
        self.secret, self.wire, self._http = secret, wire, http
        self._inproc = {p: InProcTransport(self.inbox, secret=secret) for p in PEERS}
        self._live: dict[str, _Live] = {}

    async def send(self, msg: Message) -> None:
        await (self._http or self._inproc[msg.receiver]).send(msg)

    async def run_task(self, task: Mapping[str, Any], *, shape: str, n: int,
                       timeout: float = transport.SEND_TIMEOUT_S) -> TaskRun:
        """Pose `task` to the first `n` peers and wait for the all-gather."""
        _check(shapes=(shape,), nodes=(n,))
        peers = PEERS[:n]
        conversation_id = f"{shape}-n{n}-{task['_conversation_id']}"
        live = _Live(
            run=TaskRun(self.wire, shape, n, str(task["id"]), conversation_id),
            peers=peers, task=task,
            held={p: set() for p in peers}, clocks={p: LamportClock() for p in peers},
        )
        self._live[conversation_id] = live
        try:
            live.started = perf_counter()
            live.held[peers[0]].add(peers[0])
            if shape == "ring":
                await self._emit(live, peers[0], peers[1], {peers[0]}, None,
                                 {"first_complete": None})
            else:
                await asyncio.gather(*(self._emit(live, peers[0], w, {peers[0]}, None)
                                       for w in peers[1:]))
            with suppress(TimeoutError):
                await asyncio.wait_for(live.done.wait(), timeout)
        finally:
            self._live.pop(conversation_id, None)
        if live.error is not None:
            raise live.error
        return live.run

    async def inbox(self, msg: Message) -> None:
        arrived = perf_counter()
        live = self._live.get(msg.conversation_id)
        if live is None or not security.verify(msg, self.secret):
            return
        try:
            live.run.delivered.append((msg, (arrived - live.started) * 1000))
            me = msg.receiver
            live.clocks[me].observe(msg.lamport)
            live.held[me].update(msg.payload.get("results", {}))
            if msg.performative == _REQUEST:
                live.held[me].add(me)
            if not live.done.is_set() and all(live.has_all(p) for p in live.peers):
                live.run.wall_ms = (arrived - live.started) * 1000
                live.done.set()
            await _REACT[live.run.shape](self, live, msg)
        except Exception as exc:  # noqa: BLE001 -- re-raised by run_task, not timed out
            live.error = exc
            live.done.set()

    async def _emit(self, live: _Live, sender: str, receiver: str, results: set[str],
                    trigger: Message | None, extra: Mapping[str, Any] | None = None) -> None:
        turn = trigger.turn + 1 if trigger else 0
        complete = live.has_all(sender) if live.run.shape == "ring" else trigger is not None
        root_task = str(live.task["task"])
        msg = Message(
            protocol_version=PROTOCOL_VERSION, message_id=str(uuid4()),
            conversation_id=live.run.conversation_id,
            reply_to=trigger.message_id if trigger else None, turn=turn,
            lamport=live.clocks[sender].tick(), timestamp=transport._timestamp(),
            sender=sender, receiver=receiver, performative=_RESULT if complete else _REQUEST,
            root_task=root_task, task_status="complete" if complete else "pending",
            payload={"subtask": root_task, "constraints": list(live.task.get("constraints", [])),
                     "results": {p: f"result:{p}" for p in sorted(results)}, **(extra or {})},
            # A permanent peer is its own parent at depth 0, as in measurement 1.
            spawn={"parent": sender, "depth": 0, "max_depth": 3},
            budget={"usd_remaining": 0.50, "turns_remaining": max(0, 12 - turn)},
            context={"used": 4200, "limit": 200_000},
        )
        live.run.sent += 1
        try:
            await self.send(msg)
        except (DeliveryFailed, DeliveryRefused):
            pass  # sent and never delivered: the row's messages_lost


# --- the three routing rules -----------------------------------------------

async def _star(net: Network, live: _Live, msg: Message) -> None:
    me, hub = msg.receiver, live.peers[0]
    if me != hub:
        if msg.performative == _REQUEST:
            await net._emit(live, me, hub, {me}, msg)
        return
    if live.hub_done or not live.has_all(hub):
        return
    live.hub_done = True
    reports = {m.sender: m for m, _ in live.run.delivered if m.receiver == hub}
    await asyncio.gather(*(
        net._emit(live, hub, w, set(live.peers) - {hub, w}, reports[w])
        for w in live.peers[1:] if set(live.peers) - {hub, w}
    ))


async def _ring(net: Network, live: _Live, msg: Message) -> None:
    me = msg.receiver
    first = msg.payload.get("first_complete")
    if first is None and live.has_all(me):
        first = me
    successor = live.peers[(live.peers.index(me) + 1) % len(live.peers)]
    if successor != first:
        await net._emit(live, me, successor, set(live.held[me]), msg, {"first_complete": first})


async def _mesh(net: Network, live: _Live, msg: Message) -> None:
    me = msg.receiver
    if msg.performative == _REQUEST:
        await asyncio.gather(*(net._emit(live, me, q, {me}, msg)
                               for q in live.peers if q != me))


_REACT: dict[str, Callable[[Network, _Live, Message], Awaitable[None]]] = {
    "star": _star, "ring": _ring, "mesh": _mesh}


# --- the sweep -------------------------------------------------------------

async def measure(
    tasks: Sequence[Mapping[str, Any]], *, wire: str = "inproc",
    shapes: Sequence[str] = SHAPES, nodes: Sequence[int] = (2, 3, 4), repeat: int = 1,
    secret: str, peers: Mapping[str, Mapping[str, Any]] | None = None,
    base_port: int = DEFAULT_BASE_PORT, timeout: float = transport.SEND_TIMEOUT_S,
) -> list[TaskRun]:
    """Every task through every (shape, N), interleaved task by task, so a slow
    moment on the host lands on all conditions rather than on whichever ran first."""
    _check(shapes=shapes, nodes=nodes, wires=(wire,))

    async def sweep(net: Network) -> list[TaskRun]:
        return [await net.run_task(task, shape=shape, n=n, timeout=timeout)
                for task in transport._cycled(tasks, repeat)
                for shape in shapes for n in nodes]

    if wire == "inproc":
        return await sweep(Network(secret=secret))

    table = dict(peers) if peers is not None else {
        name: {"host": "127.0.0.1", "port": base_port + i} for i, name in enumerate(PEERS)}
    client = httpx.AsyncClient(timeout=transport.DEFAULT_HTTP_TIMEOUT)
    net = Network(secret=secret, wire=wire,
                  http=LoopbackTransport({k: dict(v) for k, v in table.items()},
                                         secret=secret, client=client))
    apps = {name: make_app(net.inbox, agent=name, peers=PEERS, secret=secret) for name in PEERS}
    servers, server_tasks = await transport._serve(apps, table)
    try:
        return await sweep(net)
    finally:
        await client.aclose()
        for server in servers:
            server.should_exit = True
        for server_task in server_tasks:
            try:
                await asyncio.wait_for(server_task, 5.0)
            except (TimeoutError, asyncio.CancelledError):
                server_task.cancel()


# --- rows ------------------------------------------------------------------

def rows_from(
    runs: Sequence[TaskRun], *, secret: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(message_rows, task_rows)`. Hop depth is read off the delivered
    envelopes' `reply_to` chains, and bytes are the signed JSON body."""
    message_rows: list[dict[str, Any]] = []
    task_rows: list[dict[str, Any]] = []
    for run in runs:
        key = {"transport": run.wire, "shape": run.shape, "n_nodes": run.n,
               "task_id": run.task_id, "conversation_id": run.conversation_id}
        depth: dict[str, int] = {}
        total_bytes = 0
        for seq, (msg, at_ms) in enumerate(run.delivered):
            # A reply is built only after its trigger arrived, so its depth is known.
            depth[msg.message_id] = 1 + depth.get(msg.reply_to or "", 0)
            n_bytes = transport.wire_bytes(msg, secret=secret)
            total_bytes += n_bytes
            message_rows.append({
                **key, "seq": seq, "sender": msg.sender, "receiver": msg.receiver,
                "performative": str(msg.performative), "lamport": msg.lamport,
                "turn": msg.turn, "hop": depth[msg.message_id], "bytes": n_bytes,
                "at_ms": f"{at_ms:.6f}",
            })
        task_rows.append({
            **key, "messages": len(run.delivered), "messages_sent": run.sent,
            "messages_lost": run.sent - len(run.delivered),
            "hops": max(depth.values(), default=0), "bytes_total": total_bytes,
            "wall_ms": "" if run.wall_ms is None else f"{run.wall_ms:.6f}",
            "completed": int(run.wall_ms is not None),
        })
    return message_rows, task_rows


def _stats(values: Sequence[float], prefix: str) -> dict[str, Any]:
    if values:
        return harness.summarize(values, prefix=prefix)
    return {f"{prefix}_{k}": "" for k in ("n", "median", "p95", "mean", "min", "max")}


def summarize_conditions(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per (transport, shape, N), ordered as SHAPES then N. Messages cover every
    task; hops and wall clock cover completed tasks only, so an empty cell means none
    completed, which is different from zero."""
    groups: dict[tuple[str, str, int], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = (str(row["transport"]), str(row["shape"]), int(row["n_nodes"]))
        _check(wires=key[:1], shapes=key[1:2])
        groups.setdefault(key, []).append(row)

    summary: list[dict[str, Any]] = []
    for wire, shape, n in sorted(groups, key=lambda k: (WIRES.index(k[0]),
                                                        SHAPES.index(k[1]), k[2])):
        group = groups[(wire, shape, n)]
        done = [r for r in group if int(r["completed"])]
        theory_messages, theory_hops = theory(shape, n)
        summary.append({
            "transport": wire, "shape": shape, "n_nodes": n,
            "n_tasks": len(group), "n_completed": len(done),
            **harness.summarize([int(r["messages"]) for r in group], prefix="messages"),
            "theory_messages": theory_messages,
            **_stats([int(r["hops"]) for r in done], "hops"),
            "theory_hops": theory_hops,
            **_stats([float(r["wall_ms"]) for r in done], "wall_ms"),
            "messages_per_completed_task":
                sum(int(r["messages_sent"]) for r in group) / len(done) if done else "",
            "messages_lost": sum(int(r["messages_lost"]) for r in group),
            "bytes_median": harness.median([int(r["bytes_total"]) for r in group]),
        })
    return summary


# --- run -------------------------------------------------------------------

async def run(
    *, nodes: int = MAX_NODES, shapes: Sequence[str] = SHAPES,
    wires: Sequence[str] = ("inproc",), repeat: int = 20, secret: str | None = None,
    tasks_path: str | None = None, min_tasks: int = harness.DEFAULT_MIN_TASKS,
    base_port: int = DEFAULT_BASE_PORT, write: bool = True, results_dir: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Sweep N = 2..`nodes`; return `(message_rows, task_rows, summary_rows)`."""
    sweep = tuple(range(MIN_NODES, nodes + 1))
    _check(shapes=shapes, nodes=(nodes,), wires=wires)

    secret = secret or os.environ.get("SAMVAD_SECRET", "")
    if not secret:
        raise ValueError("SAMVAD_SECRET is not set; every message is signed")

    tasks = transport.load_tasks(tasks_path)
    planned = len(tasks) * repeat
    if planned < min_tasks:
        raise ValueError(
            f"{planned} tasks per condition is below the floor of {min_tasks} "
            f"(docs/EXPERIMENTS.md: 'Minimum 20 tasks per condition. Below that you "
            f"are reporting noise.'). Raise --repeat to at least "
            f"{-(-min_tasks // len(tasks))}, or lower --min-tasks deliberately."
        )

    runs: list[TaskRun] = []
    for wire in wires:
        runs += await measure(tasks, wire=wire, shapes=shapes, nodes=sweep, repeat=repeat,
                              secret=secret, base_port=base_port)
    message_rows, task_rows = rows_from(runs, secret=secret)
    summary_rows = summarize_conditions(task_rows)

    if write:
        source = tasks_path or str(transport.TASKS_JSON)
        meta = {n: harness.capture_meta(
            peers=PEERS[:n], seed=0, network=transport.DEFAULT_NETWORK, source=source,
            models={p: transport.NO_MODEL for p in PEERS[:n]}).as_columns() for n in sweep}
        out = harness.RESULTS_DIR if results_dir is None else results_dir
        for name, rows in (("topology_messages", message_rows), ("topology_tasks", task_rows),
                           ("topology_summary", summary_rows)):
            harness.write_csv(name, [{**meta[int(r["n_nodes"])], **r} for r in rows],
                              results_dir=out)
    return message_rows, task_rows, summary_rows


def _cell(value: Any, spec: str) -> str:
    """An empty summary cell prints as '-', never as a zero."""
    return format(value, spec) if value != "" else "-"


def main(argv: Sequence[str] | None = None) -> int:
    parser = harness.arg_parser("topology", "Measurement 2: message complexity by topology")
    parser.add_argument("--nodes", type=int, default=MAX_NODES,
                        help=f"largest N; the sweep runs {MIN_NODES}..N (at most {MAX_NODES})")
    parser.add_argument("--shape", default=",".join(SHAPES), help=f"subset of {','.join(SHAPES)}")
    parser.add_argument("--transport", default="inproc", help=f"subset of {','.join(WIRES)}")
    parser.add_argument("--repeat", type=int, default=20,
                        help="passes over the task set, per (transport, shape, N)")
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT,
                        help="first port for the loopback receivers")
    args = parser.parse_args(argv)

    if args.network not in (harness.DEFAULT_NETWORK, transport.DEFAULT_NETWORK):
        print(f"refusing to label this run {args.network!r}: measurement 2 never crosses a LAN "
              f"(inproc and loopback run on one host); see the module docstring", file=sys.stderr)
        return 2
    shapes = tuple(s.strip() for s in args.shape.split(",") if s.strip())
    wires = tuple(w.strip() for w in args.transport.split(",") if w.strip())
    try:
        _check(shapes=shapes, nodes=(args.nodes,), wires=wires)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    _, _, summary_rows = asyncio.run(run(
        nodes=args.nodes, shapes=shapes, wires=wires, repeat=args.repeat,
        tasks_path=args.tasks, min_tasks=args.min_tasks, base_port=args.base_port,
        write=True, results_dir=args.results_dir,
    ))

    where = args.results_dir or harness.RESULTS_DIR
    print(f"-> {where}/topology_messages.csv, topology_tasks.csv, topology_summary.csv\n",
          file=sys.stderr)
    header = (f"{'wire':<9} {'shape':<5} {'N':>2} {'done':>7} {'msgs':>5} {'p95':>6} "
              f"{'theory':>6} {'hops':>5} {'theory':>6} {'wall med':>9} {'wall p95':>9} "
              f"{'msg/done':>8}")
    print(header, "-" * len(header), sep="\n", file=sys.stderr)
    for row in summary_rows:
        done = f"{row['n_completed']}/{row['n_tasks']}"
        print(f"{row['transport']:<9} {row['shape']:<5} {row['n_nodes']:>2} {done:>7} "
              f"{row['messages_median']:>5g} {row['messages_p95']:>6g} "
              f"{row['theory_messages']:>6} {_cell(row['hops_median'], 'g'):>5} "
              f"{row['theory_hops']:>6} {_cell(row['wall_ms_median'], '.3f'):>9} "
              f"{_cell(row['wall_ms_p95'], '.3f'):>9} "
              f"{_cell(row['messages_per_completed_task'], '.2f'):>8}", file=sys.stderr)
    points = len(range(MIN_NODES, args.nodes + 1))
    print(f"\nmsgs = delivered and verified per task; wall in ms, first send to all-gather.\n"
          f"N = {MIN_NODES}..{args.nodes}: {_WORDS.get(points, str(points))} N points per shape "
          f"show the shape of a curve, not enough to fit one.\n"
          f"run_network={transport.DEFAULT_NETWORK}: nothing here crossed a LAN.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
