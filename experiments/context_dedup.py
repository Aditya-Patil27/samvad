# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Measurement 4: tokens per task, inline vs refs

Method: docs/EXPERIMENTS.md#4

Writes CSV to results/. Every plot in the report regenerates from that CSV --
no hand-typed numbers. Record git SHA, date, network, model per peer, node
count. Report median AND p95: LLM latency is heavily skewed and a mean hides
it. Minimum 20 tasks per condition.

    python -m experiments.context_dedup --tasks experiments/tasks.json

Two conditions over one identical log -- the same messages, the same Lamport
order, the same reported costs. Only artifact carriage changes:

* `inline` -- full artifact bytes ride in every message of the conversation
  from the moment the artifact exists. This is the failure docs/PROTOCOL.md
  names: "Four agents holding the same bytes is how a 4x200K pool collapses
  to 200K."
* `refs` -- a `sha256:` reference rides instead, and the receiver fetches
  `GET /blob/{hash}` only if it decides it needs the bytes.

**What is measured and what is modelled.** The log's own numbers -- `cost.input`,
`cost.usd`, `context.used`, `artifacts[].tokens`, and the serialized size of
every envelope -- are read straight off the frames. Three things are not in the
log and are modelled here, deliberately and visibly:

1. **Artifact bytes on the wire.** The fixture carries a token count, not bytes.
   Sized at `BYTES_PER_TOKEN` per token. Read those columns as a ratio between
   the conditions, not as an absolute byte count.
2. **Repeat carriage under `inline`.** The log is a `refs` log -- it is what the
   protocol specifies -- so `inline` is a counterfactual replay of it, not a
   second run. It re-charges the receiving window for the bytes on every hop
   they would have travelled.
3. **Who hydrates.** Nothing in the envelope records a `GET /blob/{hash}`.
   `will_act_later()` is the stand-in: an agent pays for the bytes when it still
   has work to do in that conversation, and lives on the summary when it does
   not. It is a policy, swappable in one argument, and what it produces on this
   fixture is reported as a policy output, not as an observation.

On this fixture that policy yields **zero fetches and one skip per task**:
`fixtures.py` attaches an artifact only to the terminal `task_result` of a
completed task, so an artifact is always announced on the last hop of its
conversation, to a coordinator with nothing left to do. That is a property of
the fixture, not of the mechanism, and it is why `extra_round_trips` -- the
honest cost of refs -- is 0 in every row of this CSV. tests/test_measurements.py
drives the fetching path on a hand-built log so the mechanism is tested even
though this data set never triggers it. Say both things in the report.

**This module deliberately does not import `samvad.protocol`.** Same reason
fixtures.py does not: frozen week-1 joint task, and a measurement that waits for
it arrives after the report.
"""
from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from experiments import fixtures, harness

CONDITIONS: tuple[str, ...] = ("inline", "refs")

#: Bytes per token for artifact payloads. The fixture records `tokens`, not
#: bytes; ~4 bytes/token is the usual figure for source text. Every byte column
#: inherits this assumption -- read those columns as a ratio, not a size.
BYTES_PER_TOKEN = 4

#: Envelope keys the dashboard reads that are not protocol fields. They do not
#: cross a wire, so they do not count toward bytes on the wire.
_NOT_ON_THE_WIRE = frozenset(fixtures.DISPLAY_ONLY) | {"artifacts"}

HydrationPolicy = Callable[[str, frozenset[str]], bool]


def will_act_later(receiver: str, later_senders: frozenset[str]) -> bool:
    """Default hydration policy: fetch the bytes if there is work left to do.

    A receiver that speaks again in this conversation is continuing the task and
    needs what the artifact contains. A receiver that does not is holding a
    finished result, and `summary` plus `exit_code` is what it decides from --
    docs/PROTOCOL.md sizes the `summary` field for exactly that decision.

    This is the one genuinely unobservable step in the measurement: a blob fetch
    is an HTTP GET that never appears in the message log. Swap this function to
    ask a different question of the same data.
    """
    return receiver in later_senders


# --- one condition over one log --------------------------------------------

def simulate(
    messages: Sequence[Mapping[str, Any]], condition: str, *,
    policy: HydrationPolicy = will_act_later,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay `messages` under one artifact-carriage condition.

    Returns `(task_rows, agent_rows)`: one row per task, and one row per
    `(task, agent)`. Both are needed. docs/EXPERIMENTS.md#4 asks for "peak
    `context.used` **per agent**", and a per-task maximum hides exactly what the
    mechanism moves -- on this log the busiest agent is a worker that never
    receives an artifact, so the task-level peak is identical under both
    conditions while the receiving agent's window is not.
    """
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}, expected one of {CONDITIONS}")
    order: list[str] = []
    by_conversation: dict[str, list[Mapping[str, Any]]] = {}
    for m in messages:
        conv = m["conversation_id"]
        if conv not in by_conversation:
            order.append(conv)
            by_conversation[conv] = []
        by_conversation[conv].append(m)

    task_rows: list[dict[str, Any]] = []
    agent_rows: list[dict[str, Any]] = []
    for conv in order:
        task, agents = _task_row(conv, by_conversation[conv], condition, policy)
        task_rows.append(task)
        agent_rows.extend(agents)
    return task_rows, agent_rows


def _task_row(
    conversation_id: str, msgs: Sequence[Mapping[str, Any]], condition: str,
    policy: HydrationPolicy,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One task, one condition. Ordered by `(lamport, sender)` -- never by
    `timestamp`: four devices, four clocks, and the fixture's skew is there
    precisely so that sorting on wall time gives a visibly wrong answer."""
    ordered = sorted(msgs, key=lambda m: (m["lamport"], m["sender"]))
    later_senders = _later_senders(ordered)

    carried: list[Mapping[str, Any]] = []
    seen_refs: set[str] = set()
    overhead: dict[str, int] = {}
    received: dict[str, int] = {}
    held: dict[str, set[str]] = {}
    base_used: dict[str, int] = {}
    wire_bytes = 0
    fetches = skipped = 0
    artifact_tokens = 0

    for i, msg in enumerate(ordered):
        sender_host = fixtures.device(msg["sender"])
        receiver_host = fixtures.device(msg["receiver"])
        used = (msg.get("context") or {}).get("used", 0)
        base_used[sender_host] = max(base_used.get(sender_host, 0), used)
        overhead.setdefault(sender_host, 0)
        overhead.setdefault(receiver_host, 0)

        announced = list(msg.get("artifacts") or [])
        for artifact in announced:
            if artifact["ref"] not in seen_refs:
                seen_refs.add(artifact["ref"])
                artifact_tokens += int(artifact["tokens"])
        visible = carried + announced
        wire_bytes += _envelope_bytes(msg)
        received[receiver_host] = received.get(receiver_host, 0) + len(visible)

        if condition == "inline":
            # Full bytes on every hop -- charged to the receiving window each time.
            wire_bytes += sum(int(a["tokens"]) * BYTES_PER_TOKEN for a in visible)
            overhead[receiver_host] += sum(int(a["tokens"]) for a in visible)
        else:
            wire_bytes += sum(_ref_bytes(a) for a in visible)
            overhead[receiver_host] += sum(_ref_tokens(a) for a in visible)
            mine = held.setdefault(receiver_host, set())
            for artifact in visible:
                if artifact["ref"] in mine:
                    continue           # content-addressed: already holding these bytes
                if policy(receiver_host, later_senders[i]):
                    fetches += 1
                    mine.add(artifact["ref"])
                    overhead[receiver_host] += int(artifact["tokens"])
                    wire_bytes += int(artifact["tokens"]) * BYTES_PER_TOKEN
                else:
                    skipped += 1       # bytes that never entered a second context window
        carried = visible

    agent_rows = [{
        "condition": condition,
        "conversation_id": conversation_id,
        "agent": host,
        "model": fixtures.model_for(host),
        "base_context_used": base_used.get(host, 0),
        "carriage_tokens": overhead[host],
        "peak_context_used": base_used.get(host, 0) + overhead[host],
        "context_limit": fixtures.CONTEXT_LIMIT,
        "artifacts_received": received.get(host, 0),
        "blobs_held": len(held.get(host, ())),
    } for host in sorted(overhead)]

    peaks = {row["agent"]: row["peak_context_used"] for row in agent_rows}
    peak_agent = max(peaks, key=lambda h: (peaks[h], h)) if peaks else ""
    reported_input = sum(int((m.get("cost") or {}).get("input", 0)) for m in ordered)
    reported_usd = sum(float((m.get("cost") or {}).get("usd", 0.0)) for m in ordered)
    extra_tokens = sum(overhead.values())

    task_row = {
        "condition": condition,
        "conversation_id": conversation_id,
        "n_messages": len(ordered),
        "n_agents": len(agent_rows),
        "n_artifacts": len(seen_refs),
        "artifact_tokens": artifact_tokens,
        "input_tokens_reported": reported_input,
        "input_tokens_carriage": extra_tokens,
        "input_tokens_total": reported_input + extra_tokens,
        "usd_reported": round(reported_usd, 6),
        "usd_total": round(reported_usd + _carriage_usd(overhead), 6),
        "peak_context_used": peaks[peak_agent] if peaks else 0,
        "peak_context_agent": peak_agent,
        "peak_context_receiver": max(
            (row["peak_context_used"] for row in agent_rows if row["artifacts_received"]),
            default=0),
        "context_exhausted": int(any(v >= fixtures.CONTEXT_LIMIT for v in peaks.values())),
        "blob_fetches": fetches,
        "hydrations_skipped": skipped,
        "extra_round_trips": fetches,     # one GET /blob/{hash} per hydration
        "bytes_on_wire": wire_bytes,
    }
    return task_row, agent_rows


def _later_senders(ordered: Sequence[Mapping[str, Any]]) -> list[frozenset[str]]:
    """For each position, the devices that still speak after it. Built once per
    conversation so the hydration policy is O(1) per artifact arrival."""
    out: list[frozenset[str]] = [frozenset()] * len(ordered)
    tail: set[str] = set()
    for i in range(len(ordered) - 1, -1, -1):
        out[i] = frozenset(tail)
        tail.add(fixtures.device(ordered[i]["sender"]))
    return out


def _envelope_bytes(msg: Mapping[str, Any]) -> int:
    """Canonical JSON size of the envelope without its artifacts: keys sorted,
    no whitespace -- the same canonicalisation docs/PROTOCOL.md signs over.
    Display-only fixture keys are excluded; they never cross a wire."""
    body = {k: v for k, v in msg.items() if k not in _NOT_ON_THE_WIRE}
    return len(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _ref_bytes(artifact: Mapping[str, Any]) -> int:
    return len(json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _ref_tokens(artifact: Mapping[str, Any]) -> int:
    """What the reference itself costs the receiver's window: the hash, the kind,
    the <=200 char summary and the token count it decides from."""
    return math.ceil(_ref_bytes(artifact) / BYTES_PER_TOKEN)


def _carriage_usd(overhead: Mapping[str, int]) -> float:
    """USD for the carriage tokens, at each receiving peer's own input rate.

    Charged at the full input rate: what a prompt cache would do with bytes that
    change every message is not something this log can tell us, and guessing a
    hit rate would invent the saving measurement 4 exists to measure.
    """
    total = 0.0
    for host, tokens in overhead.items():
        rate_in, _ = fixtures.MODEL_RATES[fixtures.model_for(host)]
        total += tokens * rate_in / 1_000_000
    return total


# --- the run ---------------------------------------------------------------

def summarize_conditions(
    task_rows: Sequence[Mapping[str, Any]], agent_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """One row per condition: median and p95 of every per-task series, the
    per-agent peak context the doc asks for, and the totals that only mean
    anything summed (fetches, skips, round trips)."""
    rows: list[dict[str, Any]] = []
    for condition in CONDITIONS:
        members = [r for r in task_rows if r["condition"] == condition]
        if not members:
            continue
        row: dict[str, Any] = {"condition": condition, "n_tasks": len(members)}
        for column, prefix in (
            ("input_tokens_total", "input_tokens"),
            ("peak_context_used", "peak_context"),
            ("peak_context_receiver", "peak_context_receiver"),
            ("bytes_on_wire", "bytes_on_wire"),
            ("usd_total", "usd"),
            ("artifact_tokens", "artifact_tokens"),
        ):
            stats = harness.summarize([r[column] for r in members], prefix=prefix)
            row.update({k: round(float(v), 6) for k, v in stats.items()})
        per_agent = [r["peak_context_used"] for r in agent_rows if r["condition"] == condition]
        row.update({k: round(float(v), 6)
                    for k, v in harness.summarize(per_agent, prefix="agent_peak").items()})
        row["blob_fetches_total"] = sum(r["blob_fetches"] for r in members)
        row["hydrations_skipped_total"] = sum(r["hydrations_skipped"] for r in members)
        row["extra_round_trips_total"] = sum(r["extra_round_trips"] for r in members)
        row["tasks_context_exhausted"] = sum(r["context_exhausted"] for r in members)
        rows.append(row)
    return rows


def run(
    *, min_tasks: int = harness.DEFAULT_MIN_TASKS, seed: int = harness.DEFAULT_SEED,
    n_agents: int = len(fixtures.PEERS), n_messages: int | None = None,
    network: str = harness.DEFAULT_NETWORK, tasks: str | None = None,
    policy: HydrationPolicy = will_act_later,
    write: bool = True, results_dir: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Measure both conditions over one synthetic log.

    Returns `(task_rows, agent_rows, summary_rows)`. Deterministic in
    `(min_tasks, seed, n_agents, n_messages)`. `write=False` is for tests: same
    rows, no file.
    """
    frames, _ = harness.log_with_at_least(
        min_tasks=min_tasks, seed=seed, n_agents=n_agents, n_messages=n_messages)
    messages = harness.whole_conversations(fixtures.messages(frames))
    task_rows: list[dict[str, Any]] = []
    agent_rows: list[dict[str, Any]] = []
    for condition in CONDITIONS:
        tasks_of, agents_of = simulate(messages, condition, policy=policy)
        task_rows.extend(tasks_of)
        agent_rows.extend(agents_of)
    summary_rows = summarize_conditions(task_rows, agent_rows)

    if write:
        peers = fixtures.peer_names(n_agents)
        meta = harness.capture_meta(peers=peers, seed=seed, network=network,
                                    source=tasks or "experiments.fixtures.synthetic_log")
        out = harness.RESULTS_DIR if results_dir is None else results_dir
        harness.write_csv("context_dedup_tasks", task_rows, meta=meta, results_dir=out)
        harness.write_csv("context_dedup_agents", agent_rows, meta=meta, results_dir=out)
        harness.write_csv("context_dedup_summary", summary_rows, meta=meta, results_dir=out)
    return task_rows, agent_rows, summary_rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = harness.arg_parser("context_dedup", "Measurement 4: inline vs refs")
    args = parser.parse_args(argv)
    _, _, summary_rows = run(
        min_tasks=args.min_tasks, seed=args.seed, n_agents=args.agents,
        n_messages=args.messages, network=args.network, tasks=args.tasks,
        write=True, results_dir=args.results_dir)
    where = args.results_dir or harness.RESULTS_DIR
    by = {r["condition"]: r for r in summary_rows}
    print(f"tasks: {by['refs']['n_tasks']} per condition  ->  "
          f"{where}/context_dedup_tasks.csv", file=sys.stderr)
    for condition in CONDITIONS:
        row = by[condition]
        print(f"  {condition:<7} input tokens median {row['input_tokens_median']:>10.1f} "
              f"p95 {row['input_tokens_p95']:>10.1f} | receiver peak ctx median "
              f"{row['peak_context_receiver_median']:>9.1f} | wire median "
              f"{row['bytes_on_wire_median']:>9.1f} | fetches {row['blob_fetches_total']:>3} "
              f"skipped {row['hydrations_skipped_total']:>3}", file=sys.stderr)
    saved = by["inline"]["input_tokens_median"] - by["refs"]["input_tokens_median"]
    print(f"refs saves {saved:.1f} input tokens at the median, for "
          f"{by['refs']['extra_round_trips_total']} extra round trips", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
