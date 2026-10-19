"""Contract for measurement 2 (experiments/topology.py).

Written before the implementation, per CLAUDE.md.

Like measurement 1, this drives the real transports with real signed envelopes.
A message count read off a formula would measure nothing, so the counts here
are the envelopes that actually arrived and verified. What these tests defend:

1. **The measured counts are the theory.** For every shape and N = 2, 3, 4, the
   messages delivered and the causal hop depth must equal what the routing rule
   predicts. The table is restated below, independently of the module, so the
   module cannot agree with itself by construction.
2. **Each shape uses only its own links.** A star that hit the right count by
   sending worker-to-worker would pass a count test and still not be a star.
3. **Every envelope is signed and verifies**, inproc as well as over HTTP, and a
   receiver that cannot verify a message does not count it.
4. **Stable CSV, with the run metadata on every row.** Another task plots from
   these columns. A renamed column is a silently empty plot.
5. **One harness.** Median and p95 come from experiments/harness.py.
"""
from __future__ import annotations

import csv
import json

import pytest

from experiments import harness, topology, transport
from samvad import security
from samvad.transport.inproc import InProcTransport

SECRET = "measurement-2-contract-secret"

PEERS = transport.PEERS

#: (messages, hops) for one task, from the method in the module docstring.
THEORY = {
    ("star", 2): (2, 2), ("star", 3): (6, 3), ("star", 4): (9, 3),
    ("ring", 2): (2, 2), ("ring", 3): (4, 4), ("ring", 4): (6, 6),
    ("mesh", 2): (2, 2), ("mesh", 3): (6, 2), ("mesh", 4): (12, 2),
}

META = ["run_git_sha", "run_date", "run_network", "run_node_count", "run_models",
        "run_seed", "run_source"]

MESSAGE_COLUMNS = ["transport", "shape", "n_nodes", "task_id", "conversation_id", "seq",
                   "sender", "receiver", "performative", "lamport", "turn", "hop", "bytes",
                   "at_ms"]

TASK_COLUMNS = ["transport", "shape", "n_nodes", "task_id", "conversation_id", "messages",
                "messages_sent", "messages_lost", "hops", "bytes_total", "wall_ms", "completed"]

STATS = ["n", "median", "p95", "mean", "min", "max"]

SUMMARY_COLUMNS = (
    ["transport", "shape", "n_nodes", "n_tasks", "n_completed"]
    + [f"messages_{s}" for s in STATS] + ["theory_messages"]
    + [f"hops_{s}" for s in STATS] + ["theory_hops"]
    + [f"wall_ms_{s}" for s in STATS]
    + ["messages_per_completed_task", "messages_lost", "bytes_median"]
)


def one_task() -> list[dict]:
    return transport.load_tasks(None)[:1]


def free_peer_table() -> dict[str, dict]:
    """Four distinct closed ports, so no test fights another over 8210."""
    ports: set[int] = set()
    while len(ports) < len(PEERS):
        ports.add(transport.free_port())
    return {name: {"host": "127.0.0.1", "port": port} for name, port in zip(PEERS, sorted(ports))}


def read_csv(path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def task_row(shape: str, n: int, *, messages: int = 6, hops: int = 6, wall: float = 1.0,
             completed: int = 1) -> dict:
    return {
        "transport": "inproc", "shape": shape, "n_nodes": n, "task_id": "t01",
        "conversation_id": f"{shape}-n{n}-conv-t01-r000", "messages": messages,
        "messages_sent": messages, "messages_lost": 0, "hops": hops, "bytes_total": 900,
        "wall_ms": f"{wall:.6f}" if completed else "", "completed": completed,
    }


# ==========================================================================
# the counts are the theory, measured
# ==========================================================================

@pytest.mark.parametrize(("shape", "n"), sorted(THEORY))
async def test_inproc_counts_match_the_theory(shape, n):
    runs = await topology.measure(one_task(), shapes=(shape,), nodes=(n,), repeat=1,
                                  secret=SECRET)
    _, rows = topology.rows_from(runs, secret=SECRET)
    (row,) = rows
    assert row["completed"] == 1
    assert (row["messages"], row["hops"]) == THEORY[(shape, n)]
    assert row["messages_sent"] == row["messages"]
    assert row["messages_lost"] == 0


async def test_loopback_http_counts_match_the_theory_and_every_envelope_verifies():
    """Same rule over real sockets: the server verifies, dedupes and answers 202."""
    runs = await topology.measure(one_task(), wire="loopback", shapes=topology.SHAPES,
                                  nodes=(2, 3, 4), repeat=1, secret=SECRET,
                                  peers=free_peer_table())
    _, rows = topology.rows_from(runs, secret=SECRET)
    assert {(r["shape"], r["n_nodes"]): (r["messages"], r["hops"]) for r in rows} == THEORY
    assert all(r["transport"] == "loopback" for r in rows)
    assert all(r["completed"] == 1 and r["messages_lost"] == 0 for r in rows)
    for run in runs:
        for msg, _ in run.delivered:
            assert security.verify(msg, SECRET)


async def test_at_two_nodes_the_three_shapes_send_the_same_messages():
    """One link and no routing choice. If the shapes differ here, the harness is
    measuring itself rather than the routing."""
    runs = await topology.measure(one_task(), shapes=topology.SHAPES, nodes=(2,), repeat=1,
                                  secret=SECRET)
    links = {run.shape: [(m.sender, m.receiver) for m, _ in run.delivered] for run in runs}
    assert links == {shape: [("agent_a", "agent_b"), ("agent_b", "agent_a")]
                     for shape in topology.SHAPES}


async def test_each_shape_uses_only_its_own_links():
    runs = await topology.measure(one_task(), shapes=topology.SHAPES, nodes=(4,), repeat=1,
                                  secret=SECRET)
    by_shape = {run.shape: {(m.sender, m.receiver) for m, _ in run.delivered} for run in runs}
    planner = PEERS[0]
    assert all(planner in link for link in by_shape["star"]), "a star routes via the planner"
    assert all(receiver == PEERS[(PEERS.index(sender) + 1) % 4]
               for sender, receiver in by_shape["ring"]), "a ring only talks to its successor"
    assert by_shape["mesh"] == {(s, r) for s in PEERS for r in PEERS if s != r}


async def test_conditions_are_interleaved_task_by_task():
    """A slow moment on the host must land on every shape, not on whichever ran
    first."""
    runs = await topology.measure(transport.load_tasks(None)[:2], shapes=topology.SHAPES,
                                  nodes=(2, 3), repeat=1, secret=SECRET)
    order = [(run.task_id, run.shape, run.n) for run in runs]
    first_task = order[:len(topology.SHAPES) * 2]
    assert {task for task, _, _ in first_task} == {order[0][0]}
    assert {(shape, n) for _, shape, n in first_task} == \
        {(s, n) for s in topology.SHAPES for n in (2, 3)}


# ==========================================================================
# signing
# ==========================================================================

async def test_every_delivered_envelope_is_signed_and_verifies():
    """docs/EXPERIMENTS.md#1 forbids an unsigned fast path. It applies here too."""
    runs = await topology.measure(one_task(), shapes=topology.SHAPES, nodes=(4,), repeat=1,
                                  secret=SECRET)
    delivered = [msg for run in runs for msg, _ in run.delivered]
    assert len(delivered) == sum(THEORY[(shape, 4)][0] for shape in topology.SHAPES)
    for msg in delivered:
        assert msg.sig and msg.sig.startswith(security.PREFIX)
        assert security.verify(msg, SECRET)
        assert not security.verify(msg, "not-the-secret")


async def test_a_receiver_that_cannot_verify_counts_nothing():
    """The check at the receiver is real. A message it cannot verify is lost,
    not delivered, and the task does not complete."""
    net = topology.Network(secret=SECRET, wire="inproc")
    wires = {p: InProcTransport(net.inbox, secret="a-different-secret") for p in PEERS}

    async def send(msg):
        await wires[msg.receiver].send(msg)

    net.send = send
    task = {**transport.load_tasks(None)[0], "_conversation_id": "conv-reject"}
    run = await net.run_task(task, shape="star", n=4, timeout=0.2)
    assert run.sent == 3
    assert run.delivered == []
    _, (row,) = topology.rows_from([run], secret=SECRET)
    assert row["completed"] == 0
    assert row["messages_lost"] == 3
    assert row["wall_ms"] == ""


# ==========================================================================
# the summary
# ==========================================================================

def test_the_summary_takes_median_and_p95_from_the_harness():
    values = [6, 6, 6, 6, 6, 6, 6, 6, 6, 40]
    rows = [task_row("ring", 4, messages=v, wall=float(v)) for v in values]
    (row,) = topology.summarize_conditions(rows)
    assert topology.harness is harness
    assert row["messages_median"] == harness.median(values)
    assert row["messages_p95"] == harness.p95(values)
    assert row["wall_ms_p95"] == harness.p95([float(v) for v in values])
    assert row["messages_p95"] > row["messages_median"]


def test_messages_per_completed_task_charges_failed_tasks_to_the_completed_ones():
    """Messages spent on a task that never finished still cost something."""
    rows = [task_row("mesh", 4, messages=12), task_row("mesh", 4, messages=5, completed=0)]
    (row,) = topology.summarize_conditions(rows)
    assert row["n_tasks"] == 2
    assert row["n_completed"] == 1
    assert row["messages_per_completed_task"] == pytest.approx(17.0)
    assert row["wall_ms_n"] == 1, "an unfinished task has no wall clock"


def test_the_theory_column_is_the_table_in_the_docstring():
    assert {key: topology.theory(*key) for key in THEORY} == THEORY


def test_the_summary_is_ordered_by_shape_then_n():
    rows = [task_row(shape, n) for shape in ("mesh", "ring", "star") for n in (4, 3, 2)]
    summary = topology.summarize_conditions(rows)
    assert [(r["shape"], r["n_nodes"]) for r in summary] == \
        [(s, n) for s in topology.SHAPES for n in (2, 3, 4)]


async def test_an_unknown_shape_or_node_count_is_refused():
    with pytest.raises(ValueError, match="unknown shape"):
        topology.summarize_conditions([task_row("bus", 4)])
    with pytest.raises(ValueError, match="unknown shape"):
        await topology.measure(one_task(), shapes=("bus",), nodes=(4,), secret=SECRET)
    for n in (1, 5):
        with pytest.raises(ValueError, match="nodes"):
            await topology.measure(one_task(), shapes=("star",), nodes=(n,), secret=SECRET)


# ==========================================================================
# output
# ==========================================================================

async def test_run_writes_three_csvs_with_stable_columns_and_metadata(tmp_path):
    _, _, summary = await topology.run(nodes=4, repeat=1, secret=SECRET, min_tasks=1,
                                       write=True, results_dir=str(tmp_path))
    assert sorted(p.name for p in tmp_path.glob("*.csv")) == \
        ["topology_messages.csv", "topology_summary.csv", "topology_tasks.csv"]

    for name, columns in (("topology_messages.csv", MESSAGE_COLUMNS),
                          ("topology_tasks.csv", TASK_COLUMNS),
                          ("topology_summary.csv", SUMMARY_COLUMNS)):
        header, rows = read_csv(tmp_path / name)
        assert header == META + columns, name
        for row in rows:
            # Node count is the row's own N, not the largest in the sweep.
            assert row["run_node_count"] == row["n_nodes"]
            assert row["run_network"] == "one-host"
            assert row["run_models"].count("=none") == int(row["n_nodes"])

    assert [(r["shape"], r["n_nodes"]) for r in summary] == sorted(
        THEORY, key=lambda k: (topology.SHAPES.index(k[0]), k[1]))
    for row in summary:
        assert (row["messages_median"], row["hops_median"]) == \
            (row["theory_messages"], row["theory_hops"])


def test_the_cli_runs_end_to_end_into_the_results_dir(tmp_path, capsys):
    assert topology.main(["--nodes", "4", "--shape", "star,ring,mesh", "--repeat", "1",
                          "--min-tasks", "1", "--results-dir", str(tmp_path)]) == 0
    _, summary = read_csv(tmp_path / "topology_summary.csv")
    assert len(summary) == len(THEORY)
    for row in summary:
        assert (int(float(row["messages_median"])), int(float(row["hops_median"]))) == \
            THEORY[(row["shape"], int(row["n_nodes"]))]
    assert "three N points" in capsys.readouterr().err


def test_the_cli_refuses_to_label_a_one_host_run_as_a_network():
    """Nothing here crosses a LAN. A CSV that said campus-wifi would be a lie."""
    assert topology.main(["--network", "campus-wifi", "--repeat", "1", "--min-tasks", "1"]) == 2


async def test_a_run_below_the_doc_floor_is_refused(tmp_path):
    """docs/EXPERIMENTS.md: 'Minimum 20 tasks per condition.' Overridable, never
    by accident. One task on disk keeps this true however tasks.json grows."""
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps(one_task()), encoding="utf-8")
    with pytest.raises(ValueError, match="20"):
        await topology.run(repeat=1, secret=SECRET, tasks_path=str(tasks), write=False)
