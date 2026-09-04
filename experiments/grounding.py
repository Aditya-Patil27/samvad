# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Measurement 3: grounded claims vs model_prior

Method: docs/EXPERIMENTS.md#3

Writes CSV to results/. Every plot in the report regenerates from that CSV --
no hand-typed numbers. Record git SHA, date, network, model per peer, node
count. Report median AND p95: LLM latency is heavily skewed and a mean hides
it. Minimum 20 tasks per condition.

    python -m experiments.grounding --tasks experiments/tasks.json

Two numbers come out, and the gap between them is the result:

* `grounded_ratio` -- the doc's formula, `(test_output + file_content) /
  total_claims`. `peer_report` is not in the numerator.
* `grounded_ratio_transitive` -- the same, plus every `peer_report` whose
  citation chain terminates at a `test_output` or `file_content` claim.

A `peer_report` that lands on `model_prior` after any number of hops is
ungrounded, however confident each hop sounded. That transitive collapse is
error laundering made visible, and it is what `resolve_chains` exists for.
Cycles -- two agents citing each other with nobody citing a test -- terminate
and ground nothing.

**Reference edges are inferred, not carried.** `experiments/fixtures.py` emits
claims with an `evidence_type` and prose evidence ("agent_c reported it in this
conversation") but no claim id and no citation field, and the frozen envelope in
docs/PROTOCOL.md has no field for one either. So this module derives the edge
from the log: a `peer_report` cites the claims of the message it replies to,
and failing that the claims of the most recent earlier message in the same
conversation sent from a *different* device. Deterministic, and derived from
real structure -- but it is an inference about the fixture, not a measurement of
one, and the report must say so. `resolve_chains()` takes claims with explicit
`refs`, so the day the envelope carries a citation the inference is deleted and
nothing else here changes.

**This module deliberately does not import `samvad.protocol`.** Same reason
fixtures.py does not: it is a frozen week-1 joint task and a measurement that
waits for it arrives after the report.
"""
from __future__ import annotations

import sys
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from experiments import fixtures, harness

#: The four in docs/PROTOCOL.md, and only these. An unknown fifth is a schema
#: change, not a claim to bucket somewhere plausible.
EVIDENCE_TYPES: tuple[str, ...] = ("test_output", "file_content", "peer_report", "model_prior")

#: Grounded at the root: something actually ran, or something was actually read.
DIRECT_GROUNDED: frozenset[str] = frozenset({"test_output", "file_content"})

#: Grounded only through its chain.
DERIVED = "peer_report"

#: Never grounded. The model believes it; nothing ran.
UNGROUNDED = "model_prior"

#: Role is not an envelope field. It is read off the performative, which is the
#: only place the log records who was speaking in what capacity:
#: a request is a planner's, a result is the executor's, a child's report back
#: is a reviewer's. Matches how fixtures.py picks its evidence weights.
ROLE_BY_PERFORMATIVE: dict[str, str] = {
    "task_request": "planner",
    "task_result": "executor",
    "child_result": "reviewer",
}


@dataclass(frozen=True)
class Claim:
    """One `claims[]` entry, lifted out of the log and given an id.

    `refs` are the claim ids this one cites. Empty for everything but
    `peer_report`, and possibly empty for those too -- a citation of nobody.
    """

    id: str
    evidence_type: str
    refs: tuple[str, ...] = ()
    role: str = "unknown"
    model: str = "unknown"
    agent: str = "unknown"
    conversation_id: str = ""
    text: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class Resolution:
    """What following a claim's chain to its root established."""

    claim_id: str
    grounded: bool
    # direct | transitive | model_prior | chain_to_model_prior | cycle | dangling
    status: str
    chain_depth: int     # hops to the grounded root; -1 when there is none
    root_evidence_type: str | None


# --- chain resolution ------------------------------------------------------

def resolve_chains(claims: Iterable[Claim]) -> dict[str, Resolution]:
    """Follow every `peer_report` to its root and decide what it is worth.

    Grounding is reachability, so it is computed *forwards from the roots*
    rather than by recursing down from each claim: a breadth-first sweep out of
    the `test_output` / `file_content` seeds along reversed citation edges. That
    shape is what makes a cycle a non-event -- a node already visited is not
    visited again, so two agents citing each other terminate instead of
    recursing -- and it hands out the shortest chain depth for free.
    """
    index: dict[str, Claim] = {}
    for c in claims:
        if c.evidence_type not in EVIDENCE_TYPES:
            raise ValueError(f"claim {c.id}: unknown evidence_type {c.evidence_type!r}")
        if c.id in index:
            raise ValueError(f"duplicate claim id: {c.id}")
        index[c.id] = c

    cited_by: dict[str, list[str]] = {cid: [] for cid in index}
    for c in index.values():
        for ref in c.refs:
            if ref in index:
                cited_by[ref].append(c.id)

    depth: dict[str, int] = {}
    root_type: dict[str, str] = {}
    queue: deque[str] = deque()
    for c in index.values():
        if c.evidence_type in DIRECT_GROUNDED:
            depth[c.id] = 0
            root_type[c.id] = c.evidence_type
            queue.append(c.id)

    while queue:                                  # BFS: shortest chain wins
        current = queue.popleft()
        for citer in cited_by[current]:
            if citer in depth or index[citer].evidence_type != DERIVED:
                continue
            depth[citer] = depth[current] + 1
            root_type[citer] = root_type[current]
            queue.append(citer)

    return {
        cid: _classify(index[cid], index, depth, root_type)
        for cid in index
    }


def _classify(
    claim: Claim, index: Mapping[str, Claim],
    depth: Mapping[str, int], root_type: Mapping[str, str],
) -> Resolution:
    if claim.evidence_type in DIRECT_GROUNDED:
        return Resolution(claim.id, True, "direct", 0, claim.evidence_type)
    if claim.evidence_type == UNGROUNDED:
        return Resolution(claim.id, False, "model_prior", -1, UNGROUNDED)
    if claim.id in depth:
        return Resolution(claim.id, True, "transitive", depth[claim.id], root_type[claim.id])

    reachable = _reachable(claim.id, index)
    if not any(ref in index for ref in claim.refs):
        return Resolution(claim.id, False, "dangling", -1, None)
    if claim.id in reachable:                     # cites its way back to itself
        return Resolution(claim.id, False, "cycle", -1, None)
    if any(index[n].evidence_type == UNGROUNDED for n in reachable):
        return Resolution(claim.id, False, "chain_to_model_prior", -1, UNGROUNDED)
    return Resolution(claim.id, False, "dangling", -1, None)


def _reachable(start: str, index: Mapping[str, Claim]) -> set[str]:
    """Claims reachable by following citations from `start`, `start` included
    only if a cycle leads back to it. Visited-set walk, so cycles terminate."""
    seen: set[str] = set()
    stack = [ref for ref in index[start].refs if ref in index]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(ref for ref in index[node].refs if ref in index)
    return seen


# --- lifting claims out of a log -------------------------------------------

def extract_claims(messages: Sequence[Mapping[str, Any]]) -> list[Claim]:
    """Every `claims[]` entry in the log, in Lamport order, with inferred refs.

    Ordered by `(lamport, sender)` -- never `timestamp`. Four devices, four
    clocks, and fixtures.py stamps skewed wall times precisely so that code
    sorting by the wrong key produces a visibly wrong answer.
    """
    ordered = sorted(messages, key=lambda m: (m["lamport"], m["sender"]))
    by_id = {m["message_id"]: m for m in ordered if "message_id" in m}
    claims: list[Claim] = []
    prior: dict[str, list[Mapping[str, Any]]] = {}

    for msg in ordered:
        conversation = msg.get("conversation_id", "")
        entries = msg.get("claims") or []
        if entries:
            sender = msg["sender"]
            role = ROLE_BY_PERFORMATIVE.get(msg["performative"], "unknown")
            model = (msg.get("cost") or {}).get("model") or fixtures.model_for(
                fixtures.device(sender))
            refs = tuple(_cited_ids(msg, by_id, prior.get(conversation, ())))
            for i, entry in enumerate(entries):
                claims.append(Claim(
                    id=_claim_id(msg, i),
                    evidence_type=entry["evidence_type"],
                    refs=refs if entry["evidence_type"] == DERIVED else (),
                    role=role, model=model, agent=sender,
                    conversation_id=conversation,
                    text=entry.get("claim", ""), evidence=entry.get("evidence", ""),
                ))
        prior.setdefault(conversation, []).append(msg)
    return claims


def _claim_id(msg: Mapping[str, Any], index: int) -> str:
    """`<message_id>#<n>`. The envelope has no claim id, and inventing a field
    is forbidden -- so the id is derived from two fields it already carries."""
    return f"{msg.get('message_id', msg['lamport'])}#{index}"


def _cited_ids(
    msg: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]],
    prior: Sequence[Mapping[str, Any]],
) -> list[str]:
    """The claim ids a `peer_report` in `msg` is taken to cite. See the module
    docstring: this is an inference over the log, not a field it carries."""
    replied_to = by_id.get(msg.get("reply_to") or "")
    if replied_to and replied_to.get("claims"):
        return [_claim_id(replied_to, i) for i in range(len(replied_to["claims"]))]
    host = fixtures.device(msg["sender"])
    for earlier in reversed(prior):               # most recent first
        if earlier.get("claims") and fixtures.device(earlier["sender"]) != host:
            return [_claim_id(earlier, i) for i in range(len(earlier["claims"]))]
    return []


# --- tallying --------------------------------------------------------------

def tally(
    claims: Sequence[Claim], resolutions: Mapping[str, Resolution],
) -> list[dict[str, Any]]:
    """Counts and ratios overall, by role and by model -- the breakdown the doc
    asks for. Planners are expected to score worse than executors: they reason
    about work that has not happened yet, so they have nothing to cite."""
    groups: list[tuple[str, str, list[Claim]]] = [("overall", "all", list(claims))]
    for breakdown, key_of in (("role", lambda c: c.role), ("model", lambda c: c.model)):
        for key in sorted({key_of(c) for c in claims}):
            groups.append((breakdown, key, [c for c in claims if key_of(c) == key]))
    return [_group_row(breakdown, key, members, resolutions)
            for breakdown, key, members in groups]


def _group_row(
    breakdown: str, key: str, members: Sequence[Claim],
    resolutions: Mapping[str, Resolution],
) -> dict[str, Any]:
    total = len(members)
    counts = {t: sum(1 for c in members if c.evidence_type == t) for t in EVIDENCE_TYPES}
    direct = counts["test_output"] + counts["file_content"]
    peer = [c for c in members if c.evidence_type == DERIVED]
    peer_grounded = [c for c in peer if resolutions[c.id].grounded]
    depths = [resolutions[c.id].chain_depth for c in peer_grounded]
    statuses = [resolutions[c.id].status for c in peer]
    row: dict[str, Any] = {
        "breakdown": breakdown,
        "key": key,
        "total_claims": total,
        "test_output": counts["test_output"],
        "file_content": counts["file_content"],
        "peer_report": counts["peer_report"],
        "model_prior": counts["model_prior"],
        "grounded_direct": direct,
        "peer_report_grounded": len(peer_grounded),
        "peer_report_laundered": len(peer) - len(peer_grounded),
        "peer_report_cycles": statuses.count("cycle"),
        "peer_report_dangling": statuses.count("dangling"),
        # The doc's formula, exactly: peer_report is not in the numerator.
        "grounded_ratio": _ratio(direct, total),
        # ...and the same with chains followed, which is the interesting number.
        "grounded_ratio_transitive": _ratio(direct + len(peer_grounded), total),
        "model_prior_ratio": _ratio(counts["model_prior"], total),
        # Laundering rate: hearsay that dissolves once you follow it.
        "laundered_ratio": _ratio(len(peer) - len(peer_grounded), total),
    }
    row.update(_depth_stats(depths))
    return row


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _depth_stats(depths: Sequence[int]) -> dict[str, float]:
    """median and p95 of the citation-chain depth of grounded `peer_report`
    claims. Measurement 3 has no latency to report them on -- the synthetic log
    carries no timings -- so they are applied to the one distribution this
    measurement does produce. An empty sample reports zeros with `n = 0`, not a
    fabricated centre."""
    if not depths:
        return {f"chain_depth_{k}": 0.0 for k in ("n", "median", "p95", "mean", "min", "max")}
    stats = harness.summarize(depths, prefix="chain_depth")
    return {k: round(float(v), 6) for k, v in stats.items()}


# --- the run ---------------------------------------------------------------

@dataclass
class _Args:
    seed: int = harness.DEFAULT_SEED
    min_tasks: int = harness.DEFAULT_MIN_TASKS
    messages: int | None = None
    agents: int = len(fixtures.PEERS)
    network: str = harness.DEFAULT_NETWORK
    tasks: str | None = None
    results_dir: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def run(
    *, min_tasks: int = harness.DEFAULT_MIN_TASKS, seed: int = harness.DEFAULT_SEED,
    n_agents: int = len(fixtures.PEERS), n_messages: int | None = None,
    network: str = harness.DEFAULT_NETWORK, tasks: str | None = None,
    write: bool = True, results_dir: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Measure grounding over a synthetic log; return `(claim_rows, summary_rows)`.

    Deterministic in `(min_tasks, seed, n_agents, n_messages)`. `write=False` is
    for tests: same rows, no file.
    """
    frames, _ = harness.log_with_at_least(
        min_tasks=min_tasks, seed=seed, n_agents=n_agents, n_messages=n_messages)
    messages = harness.whole_conversations(fixtures.messages(frames))
    claims = extract_claims(messages)
    resolutions = resolve_chains(claims)

    claim_rows = [{
        "claim_id": c.id,
        "conversation_id": c.conversation_id,
        "agent": c.agent,
        "role": c.role,
        "model": c.model,
        "evidence_type": c.evidence_type,
        "status": resolutions[c.id].status,
        "grounded": int(resolutions[c.id].grounded),
        "chain_depth": resolutions[c.id].chain_depth,
        "root_evidence_type": resolutions[c.id].root_evidence_type or "",
        "n_refs": len(c.refs),
        "refs": ";".join(c.refs),
    } for c in claims]
    summary_rows = tally(claims, resolutions)

    if write:
        peers = fixtures.peer_names(n_agents)
        meta = harness.capture_meta(peers=peers, seed=seed, network=network,
                                    source=tasks or "experiments.fixtures.synthetic_log")
        out = harness.RESULTS_DIR if results_dir is None else results_dir
        harness.write_csv("grounding_claims", claim_rows, meta=meta, results_dir=out)
        harness.write_csv("grounding_summary", summary_rows, meta=meta, results_dir=out)
    return claim_rows, summary_rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = harness.arg_parser("grounding", "Measurement 3: grounded-claim ratio")
    args = parser.parse_args(argv)
    _, summary_rows = run(
        min_tasks=args.min_tasks, seed=args.seed, n_agents=args.agents,
        n_messages=args.messages, network=args.network, tasks=args.tasks,
        write=True, results_dir=args.results_dir)
    overall = next(r for r in summary_rows if r["breakdown"] == "overall")
    where = args.results_dir or harness.RESULTS_DIR
    print(f"claims: {overall['total_claims']}  ->  {where}/grounding_claims.csv", file=sys.stderr)
    print(f"grounded_ratio            {overall['grounded_ratio']:.3f}", file=sys.stderr)
    print(f"grounded_ratio_transitive {overall['grounded_ratio_transitive']:.3f}", file=sys.stderr)
    print(f"laundered (peer_report collapsing to model_prior) "
          f"{overall['peer_report_laundered']}", file=sys.stderr)
    for row in summary_rows:
        if row["breakdown"] == "role":
            print(f"  role {row['key']:<9} grounded_ratio {row['grounded_ratio']:.3f} "
                  f"(transitive {row['grounded_ratio_transitive']:.3f}, "
                  f"n={row['total_claims']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
