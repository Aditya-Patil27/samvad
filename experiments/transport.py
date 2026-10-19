# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Measurement 1: inproc vs loopback vs lan -- THE THESIS, quantified

Method: docs/EXPERIMENTS.md#1

The claim in Samvad.md is that in-process multi-agent systems skip the hard
part. This measures exactly how much they skip: one workload, one envelope, one
signing path, three wires.

    python -m experiments.transport --repeat 20
    python -m experiments.transport --conditions lan --peers config/peers.yaml \
        --network campus-wifi --repeat 20

**No LLM is called here, on any condition.** The measurement isolates delivery,
so a model in the loop would add 5-60 s of skew per hop and bury the microsecond
difference this table exists to show. `run_models` therefore records `none`
rather than a model that did not run.

Two latencies are recorded, because only one of them is honest everywhere:

* `accept_ms` -- `send()` entry to `send()` return. What it costs the sender to
  hand a message off: an enqueue on `inproc`, a 202 round trip over HTTP. This
  is the per-hop latency the doc's table compares, and it is measured the same
  way on all three conditions.
* `deliver_ms` -- `send()` entry to the receiver's handler being entered. One
  way, so it needs sender and receiver to share a clock. They do under `inproc`
  and `loopback`, which run in this process. They do not under `lan`, where the
  receiver is another laptop whose clock disagrees -- that is the premise of the
  whole project. On `lan` the column is left **empty** rather than filled with a
  number that silently measures clock skew.

Retries are counted by an injected `httpx.AsyncClient` event hook rather than by
instrumenting `transport/lan.py`, which belongs to P1. `LanTransport` already
accepts a client precisely so its caller can supply one; nothing in P1's layer
changes to make this measurable.

Writes `results/transport_messages.csv` (per message) and
`results/transport_summary.csv` (per condition). Every plot in the report
regenerates from those -- no hand-typed numbers.
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx
import uvicorn
import yaml

from experiments import harness
from samvad import security
from samvad.protocol import Message
from samvad.server import make_app
from samvad.transport.inproc import InProcTransport
from samvad.transport.lan import DeliveryFailed, DeliveryRefused, LanTransport
from samvad.transport.loopback import LoopbackTransport

#: The four permanent peers. Measurement 1 does not vary node count -- that is
#: measurement 2 -- so this is fixed and every condition uses all four.
PEERS: tuple[str, ...] = ("agent_a", "agent_b", "agent_c", "agent_d")

CONDITIONS: tuple[str, ...] = ("inproc", "loopback", "lan")

#: Default task set. docs/EXPERIMENTS.md: "Use a fixed task set so runs are
#: comparable." Cycled by `--repeat` to reach the 20-task floor.
TASKS_JSON = Path(__file__).resolve().parent / "tasks.json"

#: Ports for the loopback condition. Deliberately clear of 8000-8003 (the nodes
#: in config/peers.yaml) and 8101-8103 (scripts/roundtrip_demo.py), so a
#: measurement run and a live demo can share a laptop without a port clash.
DEFAULT_BASE_PORT = 8200

#: Network label when nothing crossed one. A `lan` run must pass `--network`.
DEFAULT_NETWORK = "one-host"

#: No model participates in this measurement; see the module docstring.
NO_MODEL = "none-transport-only"

SEND_TIMEOUT_S = 10.0

#: Matches transport/lan.py's own default. Injectable because a contract test
#: for the unreachable-peer path would otherwise sit through three real connect
#: timeouts per message -- 40 s on Windows, which does not refuse a closed port
#: as promptly as Linux does. A real run leaves this alone.
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=5.0)


@dataclass(frozen=True)
class Hop:
    """One message in the fixed conversation."""

    sender: str
    receiver: str
    performative: str
    task_status: str


#: The fixed workload: a planner hands work to each of the three other peers and
#: each reports back. Six messages, all four peers, identical on every wire.
#: Every condition replays exactly this, so a difference in the table is a
#: difference in delivery and nothing else.
HOPS: tuple[Hop, ...] = (
    Hop("agent_a", "agent_b", "task_request", "pending"),
    Hop("agent_b", "agent_a", "task_result", "complete"),
    Hop("agent_a", "agent_c", "task_request", "pending"),
    Hop("agent_c", "agent_a", "task_result", "complete"),
    Hop("agent_a", "agent_d", "task_request", "pending"),
    Hop("agent_d", "agent_a", "task_result", "complete"),
)


# --- the workload ----------------------------------------------------------

def _timestamp() -> str:
    """Fixed-width ISO-8601, so every envelope serialises to the same length.

    `datetime.isoformat()` drops the fractional part entirely when microseconds
    happen to be zero, which would make one envelope in a million shorter than
    its neighbours and put a phantom outlier in the bytes column.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def build_conversation(
    task: Mapping[str, Any], *, conversation_id: str, condition: str,
) -> list[Message]:
    """The six envelopes for one task.

    `condition` is accepted and deliberately unused: docs/EXPERIMENTS.md#1
    requires the envelope to be identical on every wire, and the cheapest way to
    keep that true is for the builder to have no way to vary it. The parameter
    exists so the contract test can assert byte-identity across conditions.
    """
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition!r}; expected one of {CONDITIONS}")

    root_task = str(task["task"])
    messages: list[Message] = []
    for seq, hop in enumerate(HOPS):
        messages.append(Message(
            protocol_version="2.0",
            message_id=str(uuid4()),
            conversation_id=conversation_id,
            turn=seq,
            lamport=seq + 1,
            timestamp=_timestamp(),
            sender=hop.sender,
            receiver=hop.receiver,
            performative=hop.performative,
            root_task=root_task,
            payload={"subtask": root_task, "constraints": list(task.get("constraints", []))},
            task_status=hop.task_status,
            # A permanent peer is its own parent at depth 0; only children carry
            # a different one. Matches scripts/roundtrip_demo.py.
            spawn={"parent": hop.sender, "depth": 0, "max_depth": 3},
            budget={"usd_remaining": 0.50, "turns_remaining": 12 - seq},
            context={"used": 4200, "limit": 200_000},
        ))
    return messages


def wire_bytes(msg: Message, *, secret: str) -> int:
    """Length of the JSON body that actually goes on the wire, signed.

    Measured on a copy: signing mutates `sig`, and a message that has already
    been signed once must still be signed by the transport it is handed to.
    """
    signed = msg.model_copy(deep=True)
    signed.sig = security.sign(signed, secret)
    return len(json.dumps(signed.model_dump(mode="json")).encode("utf-8"))


def load_tasks(path: str | None) -> list[dict[str, Any]]:
    """The fixed task set. Defaults to `experiments/tasks.json`."""
    source = Path(path) if path else TASKS_JSON
    tasks = json.loads(source.read_text(encoding="utf-8"))
    if not tasks:
        raise ValueError(f"empty task set: {source}")
    return tasks


def _cycled(tasks: Sequence[Mapping[str, Any]], repeat: int) -> list[dict[str, Any]]:
    """`repeat` passes over the task set, each pass a fresh conversation.

    docs/EXPERIMENTS.md wants at least 20 tasks per condition and the checked-in
    set holds three, so the set is cycled rather than padded with new tasks --
    the same work, measured repeatedly, is what makes the conditions comparable.
    """
    out: list[dict[str, Any]] = []
    for r in range(repeat):
        for task in tasks:
            out.append({**task, "_conversation_id": f"conv-{task['id']}-r{r:03d}"})
    return out


def free_port() -> int:
    """A port nothing is listening on, for the unreachable-peer case."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def dead_peer_table() -> dict[str, dict[str, Any]]:
    """A peer table pointing at a closed port, so every send retries and fails.

    This is how the retry and loss counters are exercised without unplugging a
    laptop. Delivery failure is the measurement's headline result, so the code
    that counts it cannot go untested until demo day.
    """
    port = free_port()
    return {name: {"host": "127.0.0.1", "port": port} for name in PEERS}


def peer_table_from(path: str) -> dict[str, dict[str, Any]]:
    """The `peers:` block of a peers.yaml, for the `lan` condition."""
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    peers = config.get("peers")
    if not peers:
        raise ValueError(f"no peers: block in {path}")
    return peers


def _row(
    *, condition: str, task: Mapping[str, Any], seq: int, msg: Message, n_bytes: int,
    accept_ms: float, deliver_ms: float | None, attempts: int, outcome: str,
) -> dict[str, Any]:
    """One observation. `deliver_ms` of None becomes an empty cell, never a zero."""
    return {
        "condition": condition,
        "task_id": str(task["id"]),
        "conversation_id": msg.conversation_id,
        "seq": seq,
        "sender": msg.sender,
        "receiver": msg.receiver,
        "lamport": msg.lamport,
        "bytes": n_bytes,
        "accept_ms": f"{accept_ms:.6f}",
        "deliver_ms": "" if deliver_ms is None else f"{deliver_ms:.6f}",
        "attempts": attempts,
        "retries": max(0, attempts - 1),
        "delivered": int(outcome == "delivered"),
        "lost": int(outcome != "delivered"),
        "outcome": outcome,
    }


# --- inproc ----------------------------------------------------------------

async def measure_inproc(
    tasks: Sequence[Mapping[str, Any]], *, repeat: int = 1, secret: str,
) -> list[dict[str, Any]]:
    """Four agents, one process, asyncio queues. Still signed -- no fast path.

    Delivery cannot fail here: there is no socket to refuse, no packet to drop,
    and `send()` either enqueues or raises. Every row therefore reports one
    attempt, no retry and no loss. That is not the harness being lenient; it is
    the result the whole table exists to contrast against.
    """
    loop = asyncio.get_running_loop()
    pending: dict[str, asyncio.Future[float]] = {}

    async def inbox(msg: Message) -> None:
        arrived = perf_counter()
        future = pending.get(msg.message_id)
        if future is not None and not future.done():
            future.set_result(arrived)

    transports = {name: InProcTransport(inbox, secret=secret) for name in PEERS}

    rows: list[dict[str, Any]] = []
    for task in _cycled(tasks, repeat):
        messages = build_conversation(
            task, conversation_id=str(task["_conversation_id"]), condition="inproc")
        for seq, msg in enumerate(messages):
            n_bytes = wire_bytes(msg, secret=secret)
            # Registered before the send and removed only after the arrival:
            # InProcTransport schedules the inbox as a task rather than awaiting
            # it, so the entry has to still be here when that task finally runs.
            future = pending[msg.message_id] = loop.create_future()

            started = perf_counter()
            await transports[msg.receiver].send(msg)
            accepted = perf_counter()
            try:
                arrived = await asyncio.wait_for(future, SEND_TIMEOUT_S)
            finally:
                pending.pop(msg.message_id, None)

            rows.append(_row(
                condition="inproc", task=task, seq=seq, msg=msg, n_bytes=n_bytes,
                accept_ms=(accepted - started) * 1000,
                deliver_ms=(arrived - started) * 1000,
                attempts=1, outcome="delivered",
            ))
    return rows


# --- loopback and lan ------------------------------------------------------

async def _serve(
    apps: Mapping[str, Any], peers: Mapping[str, Mapping[str, Any]],
) -> tuple[list[uvicorn.Server], list[asyncio.Task[None]]]:
    """Bring four uvicorn servers up and wait until all of them are accepting."""
    servers = [
        uvicorn.Server(uvicorn.Config(
            apps[name], host="127.0.0.1", port=int(peers[name]["port"]), log_level="error"))
        for name in PEERS
    ]
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    for _ in range(400):
        if all(s.started for s in servers):
            return servers, tasks
        await asyncio.sleep(0.02)
    raise RuntimeError("loopback servers did not start within 8s")


async def measure_http(
    tasks: Sequence[Mapping[str, Any]], *, repeat: int = 1, secret: str,
    condition: str, peers: Mapping[str, Mapping[str, Any]] | None = None,
    base_port: int = DEFAULT_BASE_PORT, timeout: httpx.Timeout | None = None,
) -> list[dict[str, Any]]:
    """Real HTTP, real sockets, real at-least-once delivery.

    `loopback` starts the four receivers in this process, so sender and receiver
    share a clock and `deliver_ms` is meaningful. `lan` does not: the receivers
    are the other laptops running `python -m samvad.node`, their clocks are
    their own, and `deliver_ms` is left empty for every row.
    """
    if condition not in ("loopback", "lan"):
        raise ValueError(f"unknown condition: {condition!r}; expected 'loopback' or 'lan'")

    serve = condition == "loopback"
    if peers is None:
        if not serve:
            raise ValueError("the lan condition needs a peer table: pass --peers config/peers.yaml")
        peers = {name: {"host": "127.0.0.1", "port": base_port + i}
                 for i, name in enumerate(PEERS)}

    loop = asyncio.get_running_loop()
    pending: dict[str, asyncio.Future[float]] = {}

    async def inbox(msg: Message) -> None:
        arrived = perf_counter()
        future = pending.get(msg.message_id)
        if future is not None and not future.done():
            future.set_result(arrived)

    servers: list[uvicorn.Server] = []
    server_tasks: list[asyncio.Task[None]] = []
    if serve:
        apps = {name: make_app(inbox, agent=name, peers=PEERS, secret=secret) for name in PEERS}
        servers, server_tasks = await _serve(apps, peers)

    # Counts every HTTP attempt the transport makes, including its retries.
    # LanTransport retries internally and reports only success or exhaustion, so
    # without this hook a retried message is indistinguishable from a clean one.
    attempts = {"n": 0}

    async def count_attempt(_request: httpx.Request) -> None:
        attempts["n"] += 1

    client = httpx.AsyncClient(timeout=timeout or DEFAULT_HTTP_TIMEOUT,
                               event_hooks={"request": [count_attempt]})
    transport_cls = LoopbackTransport if condition == "loopback" else LanTransport
    wire = transport_cls(dict(peers), secret=secret, client=client)

    rows: list[dict[str, Any]] = []
    try:
        for task in _cycled(tasks, repeat):
            messages = build_conversation(
                task, conversation_id=str(task["_conversation_id"]), condition=condition)
            for seq, msg in enumerate(messages):
                n_bytes = wire_bytes(msg, secret=secret)
                future: asyncio.Future[float] | None = None
                if serve:
                    # See measure_inproc: the receiver's handler runs as a
                    # background task, so this entry must outlive the send.
                    future = pending[msg.message_id] = loop.create_future()

                attempts["n"] = 0
                started = perf_counter()
                try:
                    await wire.send(msg)
                except DeliveryFailed:
                    outcome = "failed"
                except DeliveryRefused:
                    outcome = "refused"
                else:
                    outcome = "delivered"
                accepted = perf_counter()

                deliver_ms: float | None = None
                if future is not None:
                    try:
                        if outcome == "delivered":
                            arrived = await asyncio.wait_for(future, SEND_TIMEOUT_S)
                            deliver_ms = (arrived - started) * 1000
                        elif not future.done():
                            future.cancel()
                    finally:
                        pending.pop(msg.message_id, None)

                rows.append(_row(
                    condition=condition, task=task, seq=seq, msg=msg, n_bytes=n_bytes,
                    accept_ms=(accepted - started) * 1000, deliver_ms=deliver_ms,
                    attempts=max(1, attempts["n"]), outcome=outcome,
                ))
    finally:
        await client.aclose()
        for server in servers:
            server.should_exit = True
        for server_task in server_tasks:
            try:
                await asyncio.wait_for(server_task, 5.0)
            except (TimeoutError, asyncio.CancelledError):
                server_task.cancel()
    return rows


# --- summary ---------------------------------------------------------------

def summarize_conditions(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per condition: latency shape, retry rate, messages lost.

    `p95_over_median` is the column that carries the argument. Near 1 means
    delivery is predictable; well above 1 means the tail is long, which is what
    an in-process system never has to handle and therefore never handles.
    """
    by_condition: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        condition = str(row["condition"])
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition: {condition!r}; expected one of {CONDITIONS}")
        by_condition.setdefault(condition, []).append(row)

    summary: list[dict[str, Any]] = []
    for condition in CONDITIONS:
        group = by_condition.get(condition)
        if not group:
            continue
        accept = [float(r["accept_ms"]) for r in group]
        deliver = [float(r["deliver_ms"]) for r in group if r["deliver_ms"] != ""]
        n = len(group)
        total_retries = sum(int(r["retries"]) for r in group)

        stats = harness.summarize(accept, prefix="accept_ms")
        median_accept = stats["accept_ms_median"]
        row: dict[str, Any] = {
            "condition": condition,
            "n_messages": n,
            "n_conversations": len({r["conversation_id"] for r in group}),
            **stats,
            "p95_over_median": (stats["accept_ms_p95"] / median_accept) if median_accept else 0.0,
            "retries_per_100": 100 * total_retries / n,
            "messages_lost": sum(int(r["lost"]) for r in group),
            "messages_delivered": sum(int(r["delivered"]) for r in group),
            "bytes_median": harness.median([int(r["bytes"]) for r in group]),
        }
        # Empty on lan by design; a zero here would read as "instant", not "unknown".
        deliver_stats = harness.summarize(deliver, prefix="deliver_ms") if deliver else {}
        for key in ("deliver_ms_n", "deliver_ms_median", "deliver_ms_p95", "deliver_ms_mean",
                    "deliver_ms_min", "deliver_ms_max"):
            row[key] = deliver_stats.get(key, "")
        summary.append(row)
    return summary


# --- run -------------------------------------------------------------------

async def run(
    *, conditions: Sequence[str] = CONDITIONS, repeat: int = 20, secret: str | None = None,
    tasks_path: str | None = None, peers_path: str | None = None,
    min_tasks: int = harness.DEFAULT_MIN_TASKS, network: str = DEFAULT_NETWORK,
    base_port: int = DEFAULT_BASE_PORT, write: bool = True, results_dir: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run each condition and return `(message_rows, summary_rows)`."""
    for condition in conditions:
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition: {condition!r}; expected one of {CONDITIONS}")

    secret = secret or __import__("os").environ.get("SAMVAD_SECRET", "")
    if not secret:
        raise ValueError("SAMVAD_SECRET is not set; every condition signs every message")

    tasks = load_tasks(tasks_path)
    planned = len(tasks) * repeat
    if planned < min_tasks:
        raise ValueError(
            f"{planned} tasks per condition is below the floor of {min_tasks} "
            f"(docs/EXPERIMENTS.md: 'Minimum 20 tasks per condition. Below that you "
            f"are reporting noise.'). Raise --repeat to at least "
            f"{-(-min_tasks // len(tasks))}, or lower --min-tasks deliberately."
        )

    peers = peer_table_from(peers_path) if peers_path else None

    rows: list[dict[str, Any]] = []
    for condition in conditions:
        if condition == "inproc":
            rows += await measure_inproc(tasks, repeat=repeat, secret=secret)
        else:
            rows += await measure_http(tasks, repeat=repeat, secret=secret,
                                       condition=condition, peers=peers, base_port=base_port)

    summary_rows = summarize_conditions(rows)

    if write:
        meta = harness.capture_meta(
            peers=PEERS, seed=0, network=network,
            source=tasks_path or str(TASKS_JSON),
            models={p: NO_MODEL for p in PEERS},
        )
        out = harness.RESULTS_DIR if results_dir is None else results_dir
        harness.write_csv("transport_messages", rows, meta=meta, results_dir=out)
        harness.write_csv("transport_summary", summary_rows, meta=meta, results_dir=out)
    return rows, summary_rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = harness.arg_parser("transport", "Measurement 1: transport comparison")
    parser.add_argument("--conditions", default=",".join(CONDITIONS),
                        help=f"comma-separated subset of {','.join(CONDITIONS)}")
    parser.add_argument("--repeat", type=int, default=20,
                        help="passes over the task set, per condition")
    parser.add_argument("--peers", default=None,
                        help="peers.yaml for the lan condition (real device addresses)")
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT,
                        help="first port for the loopback receivers")
    args = parser.parse_args(argv)

    conditions = tuple(c.strip() for c in args.conditions.split(",") if c.strip())
    network = args.network if args.network != harness.DEFAULT_NETWORK else DEFAULT_NETWORK
    if "lan" in conditions and network == DEFAULT_NETWORK:
        print("refusing to label a lan run 'one-host': pass --network campus-wifi "
              "(or hotspot / ethernet)", file=sys.stderr)
        return 2

    _, summary_rows = asyncio.run(run(
        conditions=conditions, repeat=args.repeat, tasks_path=args.tasks,
        peers_path=args.peers, min_tasks=args.min_tasks, network=network,
        base_port=args.base_port, write=True, results_dir=args.results_dir,
    ))

    where = args.results_dir or harness.RESULTS_DIR
    print(f"-> {where}/transport_messages.csv, {where}/transport_summary.csv\n", file=sys.stderr)
    header = (f"{'condition':<10} {'n':>5} {'median':>9} {'p95':>9} {'p95/med':>8} "
              f"{'retry/100':>10} {'lost':>5}")
    print(header, file=sys.stderr)
    print("-" * len(header), file=sys.stderr)
    for row in summary_rows:
        print(f"{row['condition']:<10} {row['n_messages']:>5} "
              f"{row['accept_ms_median']:>9.3f} {row['accept_ms_p95']:>9.3f} "
              f"{row['p95_over_median']:>8.2f} {row['retries_per_100']:>10.2f} "
              f"{row['messages_lost']:>5}", file=sys.stderr)
    print("\nmedian/p95 in ms, send() entry to send() return.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
