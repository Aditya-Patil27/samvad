"""Contract for measurements 3 and 4 (experiments/grounding.py, context_dedup.py).

Written before the implementations, per CLAUDE.md. These do NOT skip: both
measurements run on experiments/fixtures.py, which is finished, and neither
imports src/samvad/protocol.py -- a frozen week-1 joint task, and a measurement
that waits for it arrives after the report. What these tests defend:

1. **The transitive collapse of `peer_report`.** docs/EXPERIMENTS.md#3: "A claim
   citing another agent that cites `model_prior` is ungrounded." That is the
   interesting result -- error laundering made visible -- so it is tested
   hardest, cycles included. A resolver that hangs on a cycle or credits a
   laundered claim invalidates measurement 3 entirely.
2. **Determinism.** Same seed, same CSV.
3. **The honest cost of refs.** Measurement 4 counts hydrations *skipped* --
   bytes that never entered a second context window -- and reports the extra
   round trips just as loudly. The synthetic log never triggers a hydration (see
   the context_dedup docstring), so that path is driven here on a hand-built log.
"""
import pytest

from experiments import context_dedup, fixtures, grounding, harness

SEED = 20260904


def claim(cid: str, evidence_type: str, *refs: str, role: str = "reviewer") -> grounding.Claim:
    """A bare claim node, so the chain logic can be tested without a log."""
    return grounding.Claim(
        id=cid, evidence_type=evidence_type, refs=tuple(refs),
        role=role, model="claude-opus-5", agent="agent_a", conversation_id="conv-0001",
    )


# ==========================================================================
# measurement 3 -- chain resolution
# ==========================================================================

def test_the_root_evidence_types_ground_exactly_as_the_protocol_says():
    """test_output and file_content yes, model_prior no. docs/PROTOCOL.md#claims."""
    resolved = grounding.resolve_chains([
        claim("c1", "test_output"), claim("c2", "file_content"), claim("c3", "model_prior")])
    assert resolved["c1"].grounded and resolved["c1"].status == "direct"
    assert resolved["c2"].grounded and resolved["c2"].chain_depth == 0
    assert not resolved["c3"].grounded and resolved["c3"].status == "model_prior"


def test_peer_report_citing_a_grounded_claim_is_grounded():
    resolved = grounding.resolve_chains([
        claim("root", "test_output"),
        claim("cited", "peer_report", "root"),
    ])
    assert resolved["cited"].grounded
    assert resolved["cited"].status == "transitive"
    assert resolved["cited"].chain_depth == 1
    assert resolved["cited"].root_evidence_type == "test_output"


def test_peer_report_citing_model_prior_collapses_to_ungrounded():
    """The headline result: error laundering made visible."""
    resolved = grounding.resolve_chains([
        claim("root", "model_prior"),
        claim("cited", "peer_report", "root"),
    ])
    assert not resolved["cited"].grounded
    assert resolved["cited"].status == "chain_to_model_prior"
    assert resolved["cited"].root_evidence_type == "model_prior"


def test_a_long_peer_report_chain_is_followed_all_the_way_to_its_root():
    """Four hops of hearsay over one model_prior is still hearsay."""
    resolved = grounding.resolve_chains([
        claim("c0", "model_prior"),
        claim("c1", "peer_report", "c0"),
        claim("c2", "peer_report", "c1"),
        claim("c3", "peer_report", "c2"),
        claim("c4", "peer_report", "c3"),
    ])
    assert not any(resolved[c].grounded for c in ("c1", "c2", "c3", "c4"))
    assert all(resolved[c].status == "chain_to_model_prior" for c in ("c1", "c2", "c3", "c4"))


def test_a_long_peer_report_chain_over_test_output_stays_grounded_and_records_its_depth():
    resolved = grounding.resolve_chains([
        claim("c0", "test_output"),
        claim("c1", "peer_report", "c0"),
        claim("c2", "peer_report", "c1"),
        claim("c3", "peer_report", "c2"),
    ])
    assert all(resolved[c].grounded for c in ("c1", "c2", "c3"))
    assert [resolved[f"c{i}"].chain_depth for i in range(4)] == [0, 1, 2, 3]


@pytest.mark.parametrize("edges", [
    {"a": "a"},                                   # cites itself
    {"a": "b", "b": "a"},                         # two agents citing each other
    {"a": "b", "b": "c", "c": "a"},               # three-node ring
])
def test_a_cycle_terminates_and_grounds_nothing(edges):
    """Nobody in the ring ever cited a test. Must not hang, must not credit."""
    resolved = grounding.resolve_chains(
        [claim(node, "peer_report", ref) for node, ref in edges.items()])
    assert not any(r.grounded for r in resolved.values())
    assert {r.status for r in resolved.values()} == {"cycle"}


def test_a_cycle_with_one_grounded_exit_is_grounded():
    """A cycle is not automatically fatal -- only a cycle with no way out is."""
    resolved = grounding.resolve_chains([
        claim("root", "file_content"),
        claim("a", "peer_report", "b"),
        claim("b", "peer_report", "a", "root"),
    ])
    assert resolved["b"].grounded and resolved["b"].chain_depth == 1
    assert resolved["a"].grounded and resolved["a"].chain_depth == 2


def test_a_cycle_whose_only_exit_is_model_prior_is_ungrounded():
    resolved = grounding.resolve_chains([
        claim("root", "model_prior"),
        claim("a", "peer_report", "b"),
        claim("b", "peer_report", "a", "root"),
    ])
    assert not resolved["a"].grounded and not resolved["b"].grounded


@pytest.mark.parametrize("refs", [(), ("missing",)])
def test_a_peer_report_citing_nobody_is_dangling_not_grounded(refs):
    """A citation of nothing, or of a claim outside the log. Neither grounds."""
    resolved = grounding.resolve_chains([claim("a", "peer_report", *refs)])
    assert not resolved["a"].grounded
    assert resolved["a"].status == "dangling"


def test_a_peer_report_grounded_by_the_shorter_of_two_chains():
    resolved = grounding.resolve_chains([
        claim("root", "test_output"),
        claim("mid", "peer_report", "root"),
        claim("top", "peer_report", "mid", "root"),
    ])
    assert resolved["top"].chain_depth == 1


def test_a_wide_chain_stays_linear_and_does_not_blow_up():
    """1000 claims, half cycling: guards against an exponential walk."""
    claims = [claim("root", "test_output")]
    claims += [claim(f"c{i}", "peer_report", f"c{i - 1}" if i else "root") for i in range(500)]
    claims += [claim(f"x{i}", "peer_report", f"x{(i + 1) % 500}") for i in range(500)]
    resolved = grounding.resolve_chains(claims)
    assert resolved["c499"].grounded and resolved["c499"].chain_depth == 500
    assert not resolved["x0"].grounded


def test_resolve_chains_rejects_a_duplicate_claim_id():
    with pytest.raises(ValueError):
        grounding.resolve_chains([claim("a", "model_prior"), claim("a", "test_output")])


def test_resolve_chains_rejects_an_unknown_evidence_type():
    """Validate at boundaries: an unknown type must not silently count either way."""
    with pytest.raises(ValueError):
        grounding.resolve_chains([claim("a", "vibes")])


# --------------------------------------------------------------------------
# measurement 3 -- the ratio
# --------------------------------------------------------------------------

def test_grounded_ratio_uses_the_formula_in_the_doc():
    """grounded_ratio = (test_output + file_content) / total_claims."""
    claims = [
        claim("c1", "test_output"), claim("c2", "file_content"),
        claim("c3", "model_prior"), claim("c4", "peer_report", "c1"),
    ]
    rows = grounding.tally(claims, grounding.resolve_chains(claims))
    overall = next(r for r in rows if r["breakdown"] == "overall")
    assert overall["total_claims"] == 4
    assert overall["grounded_ratio"] == pytest.approx(0.5)


def test_the_transitive_ratio_credits_a_peer_report_only_when_its_chain_holds():
    claims = [
        claim("c1", "test_output"),
        claim("c2", "peer_report", "c1"),      # grounded via the chain
        claim("c3", "model_prior"),
        claim("c4", "peer_report", "c3"),      # laundered -- must not count
    ]
    rows = grounding.tally(claims, grounding.resolve_chains(claims))
    overall = next(r for r in rows if r["breakdown"] == "overall")
    assert overall["grounded_ratio"] == pytest.approx(0.25)
    assert overall["grounded_ratio_transitive"] == pytest.approx(0.5)
    assert overall["peer_report_laundered"] == 1


def test_tally_breaks_down_by_role_and_by_model():
    claims = [
        grounding.Claim(id="p1", evidence_type="model_prior", role="planner",
                        model="claude-opus-5", agent="agent_a", conversation_id="c"),
        grounding.Claim(id="e1", evidence_type="test_output", role="executor",
                        model="claude-haiku-4-5", agent="agent_d", conversation_id="c"),
    ]
    rows = grounding.tally(claims, grounding.resolve_chains(claims))
    by = {(r["breakdown"], r["key"]): r for r in rows}
    assert by[("role", "planner")]["grounded_ratio"] == 0.0
    assert by[("role", "executor")]["grounded_ratio"] == 1.0
    assert by[("model", "claude-haiku-4-5")]["total_claims"] == 1


# --------------------------------------------------------------------------
# measurement 3 -- against the fixture log
# --------------------------------------------------------------------------

def test_extract_claims_labels_every_claim_with_a_role_and_a_model():
    frames = fixtures.synthetic_log(n_messages=200, seed=SEED)
    claims = grounding.extract_claims(fixtures.messages(frames))
    assert claims
    assert {c.role for c in claims} <= {"planner", "executor", "reviewer"}
    assert {c.model for c in claims} <= set(fixtures.MODEL_RATES)
    assert len({c.id for c in claims}) == len(claims)


def test_extract_claims_only_accepts_the_four_documented_evidence_types():
    claims = grounding.extract_claims(
        fixtures.messages(fixtures.synthetic_log(n_messages=200, seed=SEED)))
    assert {c.evidence_type for c in claims} <= set(grounding.EVIDENCE_TYPES)


def test_the_fixture_log_actually_exercises_the_chain_code():
    """No peer_report claims in the log would make the headline number vacuous."""
    claims = grounding.extract_claims(
        fixtures.messages(fixtures.synthetic_log(n_messages=400, seed=SEED)))
    assert sum(1 for c in claims if c.evidence_type == "peer_report") > 0
    assert any(c.refs for c in claims)


def test_extracted_claims_are_ordered_by_lamport_not_by_timestamp():
    """Four devices, four clocks. fixtures.py skews wall time on purpose."""
    messages = fixtures.messages(fixtures.synthetic_log(n_messages=200, seed=SEED))
    by_lamport = [m["message_id"] for m in fixtures.by_lamport(messages)]
    by_wall = [m["message_id"] for m in sorted(messages, key=lambda m: m["timestamp"])]
    assert by_lamport != by_wall                       # the fixture's whole point
    seen: list[str] = []
    for c in grounding.extract_claims(messages):
        mid = c.id.split("#")[0]
        if mid not in seen:
            seen.append(mid)
    assert seen == [mid for mid in by_lamport if mid in set(seen)]


def test_planners_score_worse_than_executors_as_the_doc_predicts():
    """docs/EXPERIMENTS.md#3 expects this and says to report it, not fix it."""
    _, summary = grounding.run(min_tasks=20, seed=SEED, write=False)
    by = {(r["breakdown"], r["key"]): r for r in summary}
    assert by[("role", "planner")]["grounded_ratio"] < by[("role", "executor")]["grounded_ratio"]


def test_grounding_run_is_deterministic():
    assert grounding.run(min_tasks=20, seed=SEED, write=False) == \
        grounding.run(min_tasks=20, seed=SEED, write=False)


def test_grounding_run_covers_at_least_twenty_tasks():
    claim_rows, _ = grounding.run(min_tasks=20, seed=SEED, write=False)
    assert len({r["conversation_id"] for r in claim_rows}) >= 20


def test_grounding_writes_two_csvs_into_the_results_dir(tmp_path):
    assert grounding.main(
        ["--seed", str(SEED), "--min-tasks", "20", "--results-dir", str(tmp_path)]) == 0
    names = sorted(p.name for p in tmp_path.glob("*.csv"))
    assert names == ["grounding_claims.csv", "grounding_summary.csv"]
    header = (tmp_path / "grounding_summary.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("run_git_sha,run_date,run_network,run_node_count")


# ==========================================================================
# measurement 4 -- the mechanism, on a hand-built log
# ==========================================================================

ARTIFACT = {"ref": "sha256:" + "ab" * 32, "kind": "python_source",
            "summary": "binary search impl, 40 lines", "tokens": 500}


def msg(lamport: int, sender: str, receiver: str, performative: str, **over):
    """One message frame, minimal, shaped like fixtures.py's."""
    frame = {
        "type": "message", "lamport": lamport, "sender": sender, "receiver": receiver,
        "performative": performative, "conversation_id": "conv-0001",
        "message_id": f"m{lamport}", "reply_to": None, "timestamp": "2026-08-24T10:15:00Z",
        "artifacts": [], "claims": [], "cost": None, "task_status": "in_progress",
        "context": {"used": 1000, "limit": fixtures.CONTEXT_LIMIT},
    }
    frame.update(over)
    return frame


def test_a_receiver_with_work_left_hydrates_and_pays_a_round_trip():
    """The cost of refs, on a log where the mechanism actually fires."""
    log = [
        msg(1, "agent_a", "agent_b", "task_request"),
        msg(2, "agent_b", "agent_a", "task_result", artifacts=[ARTIFACT]),
        msg(3, "agent_a", "agent_b", "task_request"),   # agent_a still has work to do
    ]
    (task,), _ = context_dedup.simulate(log, "refs")
    assert task["blob_fetches"] == 1
    assert task["extra_round_trips"] == 1
    # agent_b sees the ref ride along on the third hop and has nothing left to
    # do with it, so that arrival is a skip. Both sides of the trade, one task.
    assert task["hydrations_skipped"] == 1


def test_a_receiver_holding_a_finished_result_skips_the_hydration():
    """Bytes that never entered a second context window -- the pool sharing."""
    log = [
        msg(1, "agent_a", "agent_b", "task_request"),
        msg(2, "agent_b", "agent_a", "task_result", artifacts=[ARTIFACT]),
    ]
    (task,), _ = context_dedup.simulate(log, "refs")
    assert task["blob_fetches"] == 0
    assert task["hydrations_skipped"] == 1
    assert task["extra_round_trips"] == 0


def test_a_second_arrival_of_the_same_ref_is_neither_fetched_nor_skipped_twice():
    """Content-addressed: an agent holding the bytes neither refetches nor skips."""
    log = [
        msg(1, "agent_a", "agent_b", "task_request"),
        msg(2, "agent_b", "agent_a", "task_result", artifacts=[ARTIFACT]),
        msg(3, "agent_a", "agent_a", "spawn_ack"),      # ref carried, agent_a already holds it
        msg(4, "agent_a", "agent_a", "spawn_ack"),
    ]
    (task,), agents = context_dedup.simulate(log, "refs")
    assert task["blob_fetches"] == 1
    assert task["hydrations_skipped"] == 0
    assert {r["agent"]: r["blobs_held"] for r in agents}["agent_a"] == 1


def test_inline_recharges_the_same_bytes_on_every_hop_and_refs_does_not():
    log = [
        msg(1, "agent_a", "agent_b", "task_request"),
        msg(2, "agent_b", "agent_a", "task_result", artifacts=[ARTIFACT]),
        msg(3, "agent_a", "agent_b", "task_request"),
        msg(4, "agent_b", "agent_a", "task_result"),
    ]
    (inline,), _ = context_dedup.simulate(log, "inline")
    (refs,), _ = context_dedup.simulate(log, "refs")
    assert inline["input_tokens_carriage"] == 3 * ARTIFACT["tokens"]     # three hops carry it
    assert refs["input_tokens_carriage"] < inline["input_tokens_carriage"]
    assert refs["bytes_on_wire"] < inline["bytes_on_wire"]


def test_an_unknown_condition_is_refused_rather_than_silently_measured():
    with pytest.raises(ValueError):
        context_dedup.simulate([msg(1, "agent_a", "agent_b", "task_request")], "compressed")


def test_the_hydration_policy_is_swappable():
    """It is the one modelled step; a reader must be able to re-ask it."""
    log = [
        msg(1, "agent_a", "agent_b", "task_request"),
        msg(2, "agent_b", "agent_a", "task_result", artifacts=[ARTIFACT]),
    ]
    (task,), _ = context_dedup.simulate(log, "refs", policy=lambda *_: True)
    assert task["blob_fetches"] == 1


# ==========================================================================
# measurement 4 -- against the fixture log
# ==========================================================================

@pytest.fixture(scope="module")
def dedup():
    task_rows, agent_rows, summary = context_dedup.run(min_tasks=20, seed=SEED, write=False)
    return task_rows, agent_rows, summary


def _paired(rows, key):
    return ({r[key]: r for r in rows if r["condition"] == "inline"},
            {r[key]: r for r in rows if r["condition"] == "refs"})


def test_both_conditions_are_measured_over_the_same_tasks(dedup):
    task_rows, _, _ = dedup
    inline, refs = _paired(task_rows, "conversation_id")
    assert set(inline) == set(refs)
    assert len(inline) >= 20                    # doc floor: below 20 you report noise


def test_refs_never_costs_more_input_tokens_than_inline(dedup):
    task_rows, _, _ = dedup
    inline, refs = _paired(task_rows, "conversation_id")
    assert all(refs[k]["input_tokens_total"] <= inline[k]["input_tokens_total"] for k in inline)
    assert any(refs[k]["input_tokens_total"] < inline[k]["input_tokens_total"] for k in inline)


def test_refs_lowers_the_peak_context_of_the_agent_that_receives_the_artifact(dedup):
    """The point of the mechanism, per agent as the doc asks. Per *task* the two
    conditions tie: the busiest window belongs to a worker that never receives an
    artifact -- itself worth a line in the report."""
    _, agent_rows, _ = dedup
    inline = {(r["conversation_id"], r["agent"]): r
              for r in agent_rows if r["condition"] == "inline"}
    refs = {(r["conversation_id"], r["agent"]): r for r in agent_rows if r["condition"] == "refs"}
    assert set(inline) == set(refs)
    assert all(refs[k]["peak_context_used"] <= inline[k]["peak_context_used"] for k in inline)
    receivers = [k for k in inline if inline[k]["artifacts_received"]]
    assert receivers
    assert all(refs[k]["peak_context_used"] < inline[k]["peak_context_used"] for k in receivers)


def test_inline_never_pays_a_round_trip(dedup):
    task_rows, _, _ = dedup
    inline = [r for r in task_rows if r["condition"] == "inline"]
    assert all(r["blob_fetches"] == 0 and r["extra_round_trips"] == 0 for r in inline)
    assert all(r["hydrations_skipped"] == 0 for r in inline)


def test_every_round_trip_is_a_hydration_and_the_fixture_produces_none(dedup):
    """Reported, not hidden: fixtures.py attaches an artifact only to the terminal
    task_result, so no receiver here has work left and the cost of refs never
    materialises. The mechanism is tested above; this pins what the fixture shows."""
    task_rows, _, _ = dedup
    refs = [r for r in task_rows if r["condition"] == "refs"]
    assert all(r["extra_round_trips"] == r["blob_fetches"] for r in refs)
    assert sum(r["blob_fetches"] for r in refs) == 0


def test_hydrations_skipped_is_counted_and_is_not_zero(dedup):
    """docs/EXPERIMENTS.md#4 names this: artifacts referenced but never fetched."""
    task_rows, _, _ = dedup
    refs = [r for r in task_rows if r["condition"] == "refs"]
    assert sum(r["hydrations_skipped"] for r in refs) > 0
    assert sum(r["hydrations_skipped"] for r in refs) == sum(r["n_artifacts"] for r in refs)


def test_refs_puts_fewer_bytes_on_the_wire(dedup):
    task_rows, _, _ = dedup
    inline = sum(r["bytes_on_wire"] for r in task_rows if r["condition"] == "inline")
    refs = sum(r["bytes_on_wire"] for r in task_rows if r["condition"] == "refs")
    assert refs < inline


def test_usd_is_never_below_the_cost_the_log_already_reported(dedup):
    task_rows, _, _ = dedup
    assert all(r["usd_total"] >= r["usd_reported"] for r in task_rows)
    inline, refs = _paired(task_rows, "conversation_id")
    assert all(refs[k]["usd_total"] <= inline[k]["usd_total"] for k in inline)


def test_a_task_carrying_no_artifact_measures_identically_under_both_conditions(dedup):
    """No artifact, no dedup -- a divergence here would be an invented saving."""
    task_rows, _, _ = dedup
    inline, refs = _paired(task_rows, "conversation_id")
    empty = [k for k, r in inline.items() if r["n_artifacts"] == 0]
    assert empty                                # the death scene carries none
    for k in empty:
        assert refs[k]["input_tokens_total"] == inline[k]["input_tokens_total"]
        assert refs[k]["peak_context_used"] == inline[k]["peak_context_used"]
        assert refs[k]["bytes_on_wire"] == inline[k]["bytes_on_wire"]


def test_summary_reports_median_and_p95_per_condition(dedup):
    _, _, summary = dedup
    by = {r["condition"]: r for r in summary}
    assert set(by) == set(context_dedup.CONDITIONS)
    for row in by.values():
        assert row["input_tokens_median"] > 0
        assert row["input_tokens_p95"] >= row["input_tokens_median"]
        assert row["peak_context_p95"] >= row["peak_context_median"]
        assert row["agent_peak_p95"] >= row["agent_peak_median"]


def test_the_summary_carries_the_context_exhaustion_column_the_doc_asks_for(dedup):
    """"Tasks completing before context exhaustion" is in the doc's table, so the
    column is emitted -- but on this fixture it measures the generator, not the
    mechanism: fixtures.py clamps `context.used` at the limit, so the tasks
    reporting exhaustion are those where the counter saturated, equally under
    both conditions. Pinned so the report says that instead of plotting it."""
    _, _, summary = dedup
    by = {row["condition"]: row["tasks_context_exhausted"] for row in summary}
    assert by["refs"] == by["inline"]
    assert all(row["tasks_context_exhausted"] >= 0 for row in summary)


def test_context_dedup_run_is_deterministic():
    assert context_dedup.run(min_tasks=20, seed=SEED, write=False) == \
        context_dedup.run(min_tasks=20, seed=SEED, write=False)


def test_context_dedup_writes_its_csvs_into_the_results_dir(tmp_path):
    assert context_dedup.main(
        ["--seed", str(SEED), "--min-tasks", "20", "--results-dir", str(tmp_path)]) == 0
    names = sorted(p.name for p in tmp_path.glob("*.csv"))
    assert names == ["context_dedup_agents.csv", "context_dedup_summary.csv",
                     "context_dedup_tasks.csv"]


def test_both_measurements_share_the_one_harness():
    """Built once -- measurements 1, 2 and 5 inherit it."""
    assert grounding.harness is harness is context_dedup.harness
