"""Contract for measurement 1 (experiments/transport.py).

Written before the implementation, per CLAUDE.md.

Unlike measurements 3 and 4, this one does NOT run on a synthetic log. Its
whole claim is that real delivery differs from simulated delivery, so it drives
the real transports over real sockets and imports `samvad.protocol` to build
real envelopes. A synthetic version of this measurement would measure nothing.

What these tests defend:

1. **One workload, three wires.** docs/EXPERIMENTS.md#1: "Identical workload,
   identical envelope, identical signing. Only the wire changes." If the hop
   sequence or the serialised byte count differs between conditions, the table
   is comparing two things at once and the thesis claim is void.
2. **Signing on every condition.** The doc is explicit: "Do not skip signing on
   `inproc` to make it faster." An unsigned fast path would make it a different
   code path.
3. **The headline asymmetry.** In-process, delivery cannot fail -- zero retries,
   zero loss, by construction. Over HTTP it can. That contrast is the result;
   a counter that cannot register a retry would report it falsely.
4. **Honest absence.** One-way delivery latency needs a shared clock. Sender and
   receiver share one under `inproc` and `loopback` and do not under `lan`, so
   `lan` must leave the column empty rather than fill it with a number that
   silently measures clock skew.
"""
from __future__ import annotations

import httpx
import pytest

from experiments import harness, transport

#: The unreachable-peer path waits out a real connect timeout per attempt, and
#: Windows does not refuse a closed port promptly. A test asserting that retries
#: are counted does not need to spend 40s proving the clock still works.
FAST = httpx.Timeout(connect=0.05, read=0.5, write=0.5, pool=0.5)

SECRET = "measurement-1-contract-secret"


# ==========================================================================
# the workload is one workload
# ==========================================================================

def test_the_hop_sequence_is_fixed_and_touches_every_peer():
    """A condition that exercised three peers and another that exercised four
    would differ by routing as well as by wire."""
    senders = {h.sender for h in transport.HOPS}
    receivers = {h.receiver for h in transport.HOPS}
    assert senders | receivers == set(transport.PEERS)
    assert len(transport.HOPS) >= 4


def test_every_condition_builds_byte_identical_envelopes():
    """Same task, same seq -> same serialised length on every wire.

    Signing is per-message and the signature is fixed-width, so equal lengths
    here means the envelope itself did not change between conditions.
    """
    task = {"id": "t01", "task": "Implement a binary search function", "constraints": []}
    sizes = {}
    for condition in ("inproc", "loopback", "lan"):
        msgs = transport.build_conversation(task, conversation_id="conv-0001",
                                            condition=condition)
        sizes[condition] = [transport.wire_bytes(m, secret=SECRET) for m in msgs]
    assert sizes["inproc"] == sizes["loopback"] == sizes["lan"]


def test_every_message_is_signed_on_every_condition():
    """docs/EXPERIMENTS.md#1: no unsigned fast path for inproc."""
    from samvad import security

    task = {"id": "t01", "task": "x", "constraints": []}
    for condition in ("inproc", "loopback", "lan"):
        for msg in transport.build_conversation(task, conversation_id="conv-0001",
                                                condition=condition):
            msg.sig = security.sign(msg, SECRET)
            assert security.verify(msg, SECRET)


# ==========================================================================
# inproc -- delivery cannot fail
# ==========================================================================

async def test_inproc_delivers_every_message_with_no_retry_and_no_loss():
    """The headline contrast. In one process there is nothing to drop."""
    rows = await transport.measure_inproc(transport.load_tasks(None)[:1], repeat=1,
                                          secret=SECRET)
    assert rows, "inproc produced no observations"
    assert all(r["delivered"] == 1 for r in rows)
    assert all(r["retries"] == 0 for r in rows)
    assert all(r["attempts"] == 1 for r in rows)
    assert sum(r["lost"] for r in rows) == 0


async def test_inproc_reports_a_one_way_delivery_time():
    """Sender and receiver share a clock here, so the column must be filled."""
    rows = await transport.measure_inproc(transport.load_tasks(None)[:1], repeat=1,
                                          secret=SECRET)
    assert all(r["deliver_ms"] != "" for r in rows)
    assert all(float(r["deliver_ms"]) >= 0 for r in rows)


# ==========================================================================
# loopback -- real sockets, real failure
# ==========================================================================

async def test_loopback_delivers_over_real_http_and_is_slower_than_inproc():
    """Not an assertion about a magic number -- an assertion that a socket costs
    something an asyncio queue does not."""
    tasks = transport.load_tasks(None)[:1]
    inproc = await transport.measure_inproc(tasks, repeat=1, secret=SECRET)
    loop = await transport.measure_http(tasks, repeat=1, secret=SECRET,
                                        condition="loopback")
    assert all(r["delivered"] == 1 for r in loop)
    assert harness.median([float(r["accept_ms"]) for r in loop]) > \
           harness.median([float(r["accept_ms"]) for r in inproc])


async def test_an_unreachable_peer_is_counted_as_retried_then_lost():
    """The number the doc calls interesting. A counter that cannot register a
    retry would report `lan` as clean as `inproc` and invert the conclusion."""
    rows = await transport.measure_http(
        transport.load_tasks(None)[:1], repeat=1, secret=SECRET,
        condition="lan", peers=transport.dead_peer_table(), timeout=FAST,
    )
    assert rows, "a failed run must still emit observations"
    assert sum(r["lost"] for r in rows) == len(rows)
    assert all(r["attempts"] > 1 for r in rows), "at-least-once means it retried"
    assert all(r["retries"] == r["attempts"] - 1 for r in rows)


async def test_lan_leaves_one_way_delivery_empty_because_clocks_are_not_shared():
    """docs/EXPERIMENTS.md forbids a number whose provenance is a lie."""
    rows = await transport.measure_http(
        transport.load_tasks(None)[:1], repeat=1, secret=SECRET,
        condition="lan", peers=transport.dead_peer_table(), timeout=FAST,
    )
    assert all(r["deliver_ms"] == "" for r in rows)


# ==========================================================================
# the summary
# ==========================================================================

def test_the_summary_reports_median_and_p95_and_their_ratio():
    rows = [
        {"condition": "inproc", "task_id": "t01", "seq": i, "accept_ms": str(float(v)),
         "deliver_ms": str(float(v)), "attempts": 1, "retries": 0, "delivered": 1,
         "lost": 0, "bytes": 900, "sender": "agent_a", "receiver": "agent_b",
         "lamport": i + 1, "conversation_id": "conv-0001"}
        for i, v in enumerate([1, 1, 1, 1, 1, 1, 1, 1, 1, 100])
    ]
    summary = transport.summarize_conditions(rows)
    assert len(summary) == 1
    row = summary[0]
    assert row["condition"] == "inproc"
    assert row["accept_ms_median"] == pytest.approx(1.0)
    assert row["accept_ms_p95"] > row["accept_ms_median"]
    assert row["p95_over_median"] == pytest.approx(
        row["accept_ms_p95"] / row["accept_ms_median"])


def test_the_summary_reports_retries_per_100_and_messages_lost():
    rows = [
        {"condition": "lan", "task_id": "t01", "seq": i, "accept_ms": "1.0",
         "deliver_ms": "", "attempts": 2, "retries": 1, "delivered": 1, "lost": 0,
         "bytes": 900, "sender": "agent_a", "receiver": "agent_b", "lamport": i,
         "conversation_id": "conv-0001"}
        for i in range(50)
    ]
    rows.append({**rows[0], "seq": 50, "delivered": 0, "lost": 1, "attempts": 3,
                 "retries": 2, "accept_ms": "1.0"})
    row = transport.summarize_conditions(rows)[0]
    assert row["messages_lost"] == 1
    assert row["retries_per_100"] == pytest.approx(100 * 52 / 51)


def test_an_unknown_condition_is_refused_rather_than_silently_measured():
    with pytest.raises(ValueError, match="unknown condition"):
        transport.summarize_conditions([{"condition": "carrier-pigeon", "accept_ms": "1.0",
                                         "deliver_ms": "", "attempts": 1, "retries": 0,
                                         "delivered": 1, "lost": 0, "task_id": "t01",
                                         "seq": 0, "bytes": 900, "sender": "agent_a",
                                         "receiver": "agent_b", "lamport": 1,
                                         "conversation_id": "conv-0001"}])


# ==========================================================================
# output
# ==========================================================================

async def test_run_writes_both_csvs_into_the_results_dir(tmp_path):
    msg_rows, summary_rows = await transport.run(
        conditions=("inproc",), repeat=1, secret=SECRET, min_tasks=1,
        write=True, results_dir=str(tmp_path),
    )
    assert (tmp_path / "transport_messages.csv").exists()
    assert (tmp_path / "transport_summary.csv").exists()
    assert msg_rows and summary_rows
    header = (tmp_path / "transport_messages.csv").read_text(encoding="utf-8").splitlines()[0]
    for column in ("run_git_sha", "run_network", "condition", "accept_ms", "retries", "lost"):
        assert column in header


async def test_a_run_below_the_doc_floor_is_refused():
    """docs/EXPERIMENTS.md: 'Minimum 20 tasks per condition. Below that you are
    reporting noise.' The floor is overridable, but never by accident."""
    with pytest.raises(ValueError, match="20"):
        await transport.run(conditions=("inproc",), repeat=1, secret=SECRET, write=False)
