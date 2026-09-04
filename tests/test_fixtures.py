"""Contract for the synthetic message-log generator (experiments/fixtures.py).

Written before the implementation, per CLAUDE.md. These do NOT skip: fixtures.py
depends on nothing else in the repo -- deliberately, since it has to work before
protocol.py exists -- so there is no stub to wait on.

What these tests actually defend:

1. **Determinism.** Every downstream measurement is worthless if the fixture
   drifts between runs. A fixed seed must give byte-identical frames.
2. **Lamport ordering.** Four devices, four clocks. The generator deliberately
   emits timestamps that disagree with Lamport order, so any downstream code
   that sorts by wall clock produces a visibly wrong answer instead of a
   plausible one. The disagreement is asserted here, not merely hoped for.
3. **The two moments worth demoing** -- a budget-sliced fan-out, and a peer
   dying mid-flight with its orphans reparented -- survive the port from
   dashboard/index.html's demo() into Python.
"""
from experiments import fixtures

SEED = 20260904


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------

def test_synthetic_log_is_deterministic_under_a_fixed_seed():
    """Two runs at one seed are identical. Without this every CSV is noise."""
    a = fixtures.synthetic_log(seed=SEED)
    b = fixtures.synthetic_log(seed=SEED)
    assert a == b
    assert a is not b


def test_a_different_seed_gives_a_different_log():
    """Seeded, not hardcoded -- otherwise 'seed' is decoration."""
    assert fixtures.synthetic_log(seed=1) != fixtures.synthetic_log(seed=2)


def test_scripted_run_is_deterministic_and_takes_no_seed():
    """The ported demo() scenario is a fixed script, not a sample."""
    assert fixtures.scripted_run() == fixtures.scripted_run()


# --------------------------------------------------------------------------
# frame shape -- what the dashboard's /events ingest expects
# --------------------------------------------------------------------------

def test_every_frame_is_a_node_or_a_message_frame():
    for frame in fixtures.synthetic_log(n_messages=60, seed=SEED):
        assert frame["type"] in ("node", "message")


def test_message_frames_carry_the_keys_the_dashboard_reads():
    for m in fixtures.messages(fixtures.synthetic_log(n_messages=60, seed=SEED)):
        assert isinstance(m["lamport"], int)
        assert isinstance(m["sender"], str) and m["sender"]
        assert isinstance(m["receiver"], str) and m["receiver"]
        assert isinstance(m["performative"], str) and m["performative"]
        assert m["cost"] is None or isinstance(m["cost"]["usd"], float)


def test_node_frames_carry_the_keys_the_dashboard_reads():
    nodes = fixtures.nodes(fixtures.synthetic_log(n_messages=60, seed=SEED))
    assert nodes, "a log with no node frames leaves the dashboard's strip empty"
    for n in nodes:
        assert n["agent"] in fixtures.peer_names(4)
        # Node frames are sparse updates: absent key means unchanged, which is
        # exactly how the dashboard's onNode() reads them.
        if "context" in n:
            assert n["context"]["used"] <= n["context"]["limit"]


def test_message_frames_are_envelope_shaped_so_the_swap_to_protocol_is_mechanical():
    """Every field docs/PROTOCOL.md marks required is present, minus `sig`.

    fixtures.py must not import samvad.protocol (it has to work before the
    envelope exists), but the day it does exist the mapping should be
    `Message(**{k: v for k, v in frame.items() if k != "type"})` and nothing more.
    """
    required = {
        "protocol_version", "message_id", "conversation_id", "turn", "lamport",
        "timestamp", "sender", "receiver", "performative", "root_task", "payload",
        "artifacts", "claims", "spawn", "budget", "context", "cost", "task_status",
    }
    for m in fixtures.messages(fixtures.synthetic_log(n_messages=40, seed=SEED)):
        assert required <= set(m), f"missing {required - set(m)}"


def test_root_task_is_immutable_within_a_conversation():
    by_conv: dict[str, set[str]] = {}
    for m in fixtures.messages(fixtures.synthetic_log(n_messages=120, seed=SEED)):
        by_conv.setdefault(m["conversation_id"], set()).add(m["root_task"])
    assert all(len(tasks) == 1 for tasks in by_conv.values())


# --------------------------------------------------------------------------
# ordering -- Lamport, never wall clock
# --------------------------------------------------------------------------

def test_by_lamport_sorts_by_lamport_then_sender():
    ordered = fixtures.by_lamport(fixtures.synthetic_log(n_messages=120, seed=SEED))
    keys = [(m["lamport"], m["sender"]) for m in ordered]
    assert keys == sorted(keys)


def test_timestamps_deliberately_disagree_with_lamport_order():
    """The trap. Four devices means four clocks that drift apart.

    If sorting by `timestamp` gave the same answer as sorting by `lamport`,
    this fixture would silently bless code that orders by wall clock -- and the
    bug would only surface on real hardware, in the demo.
    """
    frames = fixtures.synthetic_log(n_messages=120, seed=SEED)
    by_clock = [m["message_id"] for m in fixtures.by_lamport(frames)]
    by_wall = [
        m["message_id"]
        for m in sorted(fixtures.messages(frames), key=lambda m: m["timestamp"])
    ]
    assert by_clock != by_wall


def test_lamport_respects_causality():
    """A reply always carries a higher clock than the message it answers."""
    frames = fixtures.messages(fixtures.synthetic_log(n_messages=120, seed=SEED))
    seen: dict[str, int] = {m["message_id"]: m["lamport"] for m in frames}
    replies = [m for m in frames if m.get("reply_to") in seen]
    assert replies, "no reply_to chains -- causality is untested"
    for m in replies:
        assert m["lamport"] > seen[m["reply_to"]]


# --------------------------------------------------------------------------
# moment 1 -- budget-sliced fan-out
# --------------------------------------------------------------------------

def test_a_fan_out_appears():
    acks = [
        m for m in fixtures.messages(fixtures.synthetic_log(seed=SEED))
        if m["performative"] == "spawn_ack"
    ]
    assert acks, "no spawn_ack -- the fan-out moment is missing"
    widths: dict[str, int] = {}
    for a in acks:
        widths[a["sender"]] = widths.get(a["sender"], 0) + 1
    assert max(widths.values()) >= 2, "a fan-out of one is not a fan-out"


def test_child_slices_never_exceed_the_parent_budget():
    """Budget conservation is what terminates recursion. Fake data that
    violates it would let a downstream check pass against impossible input."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for m in fixtures.messages(fixtures.synthetic_log(seed=SEED)):
        if m["performative"] == "spawn_ack":
            groups.setdefault((m["conversation_id"], m["sender"]), []).append(m)

    assert groups
    for acks in groups.values():
        parent_usd = acks[0]["budget"]["usd_remaining"]
        parent_turns = acks[0]["budget"]["turns_remaining"]
        slices = [a["payload"]["slice"] for a in acks]
        assert sum(s["usd_remaining"] for s in slices) <= parent_usd
        assert sum(s["turns_remaining"] for s in slices) <= parent_turns


def test_children_are_addressed_below_their_parent_and_carry_the_matching_depth():
    for m in fixtures.messages(fixtures.synthetic_log(seed=SEED)):
        if m["performative"] != "spawn_ack":
            continue
        path = m["payload"]["agent_path"]
        assert path.startswith(m["sender"] + "/")
        assert m["payload"]["slice"]["depth"] == path.count("/")
        assert path.count("/") <= m["spawn"]["max_depth"]


def test_children_report_back_to_their_parent():
    frames = fixtures.messages(fixtures.synthetic_log(seed=SEED))
    spawned = {
        m["payload"]["agent_path"] for m in frames if m["performative"] == "spawn_ack"
    }
    reported = {m["sender"] for m in frames if m["performative"] == "child_result"}
    assert spawned & reported, "children spawn but never report -- no result path"


# --------------------------------------------------------------------------
# moment 2 -- a peer dies mid-flight, orphans reparent
# --------------------------------------------------------------------------

def test_a_peer_goes_down_mid_run():
    frames = fixtures.synthetic_log(seed=SEED)
    down = [n for n in fixtures.nodes(frames) if n.get("up") is False]
    assert down, "no peer dies -- the failure moment is missing"
    # Mid-flight, not at the end: work must still follow the death.
    death_at = frames.index(down[0])
    assert any(f["type"] == "message" for f in frames[death_at:])


def test_orphans_are_reparented_after_the_death():
    frames = fixtures.synthetic_log(seed=SEED)
    down = next(n for n in fixtures.nodes(frames) if n.get("up") is False)
    victim = down["agent"]
    after = frames[frames.index(down):]
    orphans = [
        m for m in fixtures.messages(after)
        if m["payload"].get("reparented_from") == victim
    ]
    assert orphans, "the victim's children vanish instead of reparenting"
    for o in orphans:
        assert o["performative"] == "child_result"
        assert o["payload"]["reparented_to"] != victim
        assert o["dashed"] is True  # the dashboard draws a reparented hop dashed


# --------------------------------------------------------------------------
# parameterisation -- the five experiments each need a different shape
# --------------------------------------------------------------------------

def test_message_count_is_exact():
    for n in (12, 40, 200):
        assert len(fixtures.messages(fixtures.synthetic_log(n, seed=SEED))) == n


def test_peer_count_is_parameterised():
    for n_agents in (2, 3, 4, 6):
        frames = fixtures.synthetic_log(80, n_agents, seed=SEED)
        peers = {
            fixtures.device(m[end])
            for m in fixtures.messages(frames)
            for end in ("sender", "receiver")
        }
        assert peers <= set(fixtures.peer_names(n_agents))
        assert len(peers) == n_agents


def test_fan_out_width_is_parameterised():
    for width in (2, 4, 8):
        acks = [
            m for m in fixtures.messages(fixtures.synthetic_log(200, seed=SEED, fan_out=width))
            if m["performative"] == "spawn_ack"
        ]
        widths: dict[tuple[str, str], int] = {}
        for a in acks:
            key = (a["conversation_id"], a["sender"])
            widths[key] = widths.get(key, 0) + 1
        assert max(widths.values()) == width


def test_depth_is_parameterised_and_capped():
    for max_depth in (1, 2, 3):
        frames = fixtures.synthetic_log(200, seed=SEED, max_depth=max_depth)
        deepest = max(m["spawn"]["depth"] for m in fixtures.messages(frames))
        assert deepest <= max_depth
        for m in fixtures.messages(frames):
            assert m["spawn"]["max_depth"] == max_depth


def test_evidence_types_are_mixed_so_measurement_3_has_something_to_count():
    kinds = {
        c["evidence_type"]
        for m in fixtures.messages(fixtures.synthetic_log(200, seed=SEED))
        for c in m["claims"]
    }
    assert {"test_output", "file_content", "peer_report", "model_prior"} <= kinds


def test_costs_are_plausible_and_only_on_results():
    for m in fixtures.messages(fixtures.synthetic_log(200, seed=SEED)):
        if m["cost"] is None:
            continue
        assert m["performative"] in ("task_result", "child_result")
        assert m["cost"]["usd"] >= 0.0
        assert m["cost"]["model"] in fixtures.MODEL_RATES
        assert m["cost"]["cache_read"] <= m["cost"]["input"]


def test_bad_parameters_are_rejected_at_the_boundary():
    import pytest

    for kwargs in (
        {"n_messages": 0},
        {"n_agents": 1},
        {"fan_out": 0},
        {"max_depth": 0},
    ):
        with pytest.raises(ValueError):
            fixtures.synthetic_log(**kwargs)


# --------------------------------------------------------------------------
# the port itself -- scripted_run() must still be dashboard/index.html's demo()
# --------------------------------------------------------------------------

def test_scripted_run_reproduces_the_sixteen_step_scenario():
    """Transcribed from demo() in dashboard/index.html. If someone edits the
    scenario in one place, this fails and forces the two back into agreement."""
    msgs = fixtures.messages(fixtures.scripted_run())
    assert [m["lamport"] for m in msgs] == [
        12, 13, 15, 16, 18, 20, 21, 23, 24, 25, 28, 30, 31, 33, 35, 38
    ]
    assert [m["performative"] for m in msgs] == [
        "task_request", "task_request", "spawn_ack", "spawn_ack", "task_result",
        "task_result", "task_request", "spawn_ack", "spawn_ack", "spawn_ack",
        "task_result", "task_request", "task_result", "child_result",
        "task_request", "task_result",
    ]


def test_scripted_run_opens_with_one_node_frame_per_peer():
    frames = fixtures.scripted_run()
    opening = frames[:4]
    assert [n["agent"] for n in opening] == list(fixtures.PEERS)
    assert all(n["type"] == "node" and n["up"] is True for n in opening)
    assert {n["model"] for n in opening} == {
        "claude-opus-5", "ollama:qwen2.5-coder", "claude-haiku-4-5"
    }


def test_scripted_run_keeps_the_three_lens_fan_out():
    kids = [
        m["child"] for m in fixtures.messages(fixtures.scripted_run())
        if m["sender"] == "agent_c" and m["performative"] == "spawn_ack"
    ]
    assert kids == [
        "c/verifier_1 correctness", "c/verifier_2 edges", "c/verifier_3 execution"
    ]


def test_scripted_run_keeps_the_peer_death_and_the_reparent():
    frames = fixtures.scripted_run()
    down = [n for n in fixtures.nodes(frames) if n.get("up") is False]
    assert [n["agent"] for n in down] == ["agent_b"]

    msgs = fixtures.messages(frames)
    assert any(m.get("note") == "✕ AGENT_B DOWN" for m in msgs)
    reparent = next(m for m in msgs if m["lamport"] == 33)
    assert reparent["performative"] == "child_result"
    assert reparent["dashed"] is True
    assert reparent["note"] == "orphans reparented"


def test_scripted_run_still_disagrees_about_wall_clock():
    """Same trap as the generated log: the ported scenario must not tempt
    anyone into ordering the demo by timestamp either."""
    frames = fixtures.scripted_run()
    by_clock = [m["lamport"] for m in fixtures.by_lamport(frames)]
    by_wall = [
        m["lamport"] for m in sorted(fixtures.messages(frames), key=lambda m: m["timestamp"])
    ]
    assert by_clock != by_wall
