"""Contract: the envelope. Owner P1, but everyone depends on it.

These are the executable form of docs/PROTOCOL.md and the only thing that
catches a confident, incompatible reading of the schema.

The whole module skips itself until protocol.py carries `Message`, then turns
itself on. Nobody has to remember to delete a marker.

Rejections assert on the schema's own error type, never on bare `Exception` --
a blind assert also passes when the test itself has a typo, which is exactly
the failure these tests exist to catch.

Lamport ordering lives in test_clock.py: it needs no envelope, so it is not
gated behind this file's skip.

If a test here disagrees with docs/PROTOCOL.md, the test is wrong. Fix the
test, or bump the protocol version -- never loosen validation to reach green.
"""
import pytest

from tests.support import envelope, has_message, implemented, now_iso, schema_error, sha256_ref

pytestmark = pytest.mark.skipif(
    not has_message(),
    reason="week 1 joint task: write protocol.py together first",
)

INVALID = schema_error() if has_message() else Exception


def _Message():
    from samvad.protocol import Message

    return Message


def _make(**overrides):
    return _Message()(**envelope(**overrides))


# --- identity and versioning ------------------------------------------------


def test_version_mismatch_is_rejected_not_coerced():
    """A MAJOR mismatch is rejected. Do not coerce, do not best-effort parse.

    Silent schema drift is the most likely way this project fails; a loud
    rejection is the point.
    """
    with pytest.raises(INVALID):
        _make(protocol_version="1.0")


def test_valid_envelope_round_trips_through_json():
    """Serialise and parse back without losing or renaming a field."""
    msg = _make()
    again = _Message().model_validate_json(msg.model_dump_json())
    assert again.message_id == msg.message_id
    assert again.root_task == msg.root_task
    assert again.budget.usd_remaining == msg.budget.usd_remaining


def test_message_id_must_be_a_uuid():
    """It is the idempotency key. A non-unique format breaks dedup silently."""
    with pytest.raises(INVALID):
        _make(message_id="not-a-uuid")


# --- signing ----------------------------------------------------------------


def test_signature_roundtrip(secret):
    """sign() then verify() succeeds on an untouched message."""
    from samvad import security

    if not implemented(security.canonical, _make()):
        pytest.skip("security.canonical not implemented yet")

    msg = _make()
    msg.sig = security.sign(msg, secret)
    assert msg.sig.startswith("hmac-sha256:")
    assert security.verify(msg, secret) is True


def test_tampered_body_fails_verification(secret):
    """Changing any signed field after signing invalidates the digest."""
    from samvad import security

    if not implemented(security.canonical, _make()):
        pytest.skip("security.canonical not implemented yet")

    msg = _make()
    msg.sig = security.sign(msg, secret)
    msg.root_task = "Implement quicksort instead"
    assert security.verify(msg, secret) is False


def test_signature_excludes_sig_field_itself(secret):
    """The digest covers every field EXCEPT `sig`, or signing is circular."""
    from samvad import security

    if not implemented(security.canonical, _make()):
        pytest.skip("security.canonical not implemented yet")

    msg = _make()
    unsigned = security.canonical(msg)
    msg.sig = security.sign(msg, secret)
    assert security.canonical(msg) == unsigned


def test_canonical_form_is_key_order_independent():
    """Sorted keys, no whitespace. Two agents must not disagree on the bytes."""
    from samvad import security

    if not implemented(security.canonical, _make()):
        pytest.skip("security.canonical not implemented yet")

    fields = envelope()
    forward = _Message()(**fields)
    reversed_order = _Message()(**dict(reversed(list(fields.items()))))
    assert security.canonical(forward) == security.canonical(reversed_order)


def test_timestamp_outside_replay_window_rejected(secret):
    """Older than +/-120s is rejected as a replay, even with a valid digest."""
    from samvad import security

    if not implemented(security.canonical, _make()):
        pytest.skip("security.canonical not implemented yet")

    stale = _make(timestamp=now_iso(-(security.REPLAY_WINDOW_SECONDS + 60)))
    stale.sig = security.sign(stale, secret)
    assert security.verify(stale, secret) is False


# --- budget conservation ----------------------------------------------------


def test_budget_fields_cannot_go_negative():
    """usd_remaining and turns_remaining are >= 0 at the schema level."""
    with pytest.raises(INVALID):
        _make(budget={"usd_remaining": -0.01, "turns_remaining": 5})
    with pytest.raises(INVALID):
        _make(budget={"usd_remaining": 0.5, "turns_remaining": -1})


# --- spawn ------------------------------------------------------------------


def test_depth_cannot_exceed_max_depth():
    """The envelope-local half of the depth guard.

    Whether a child RAISED max_depth above its parent's cannot be judged here:
    it needs the parent, and one message does not carry it. That check belongs
    in the receive path and lives in tests/contract/test_transport.py. What a
    single envelope can prove is that it does not claim to sit deeper than its
    own declared ceiling.
    """
    with pytest.raises(INVALID):
        _make(
            sender="agent_a/w1/w2/w3",
            spawn={"parent": "agent_a/w1/w2", "depth": 3, "max_depth": 2},
        )


def test_spawn_parent_must_follow_the_sender_path():
    """parent is sender minus its last segment. A mismatch is a routing bug
    waiting: the address and the spawn block disagree about where the agent
    sits, and whichever the receiver trusts, the other is wrong."""
    with pytest.raises(INVALID):
        _make(
            sender="agent_a/worker_2",
            spawn={"parent": "agent_b", "depth": 1, "max_depth": 3},
        )


def test_path_depth_must_equal_spawn_depth():
    """'agent_b/worker_2' is depth 1. A mismatch is a routing bug waiting."""
    with pytest.raises(INVALID):
        _make(
            sender="agent_b/worker_2",
            spawn={"parent": "agent_b", "depth": 0, "max_depth": 3},
        )


def test_peer_paths_are_depth_zero():
    """A bare peer path carries depth 0."""
    msg = _make(sender="agent_a", spawn={"parent": "agent_a", "depth": 0, "max_depth": 3})
    assert msg.spawn.depth == 0


def test_agent_path_charset_is_enforced():
    """Paths are lowercase [a-z0-9_] per segment, '/'-separated."""
    for bad in ("Agent_B", "agent b", "agent-b", "agent_b//worker_2"):
        with pytest.raises(INVALID):
            _make(receiver=bad)


# --- task content -----------------------------------------------------------


def test_task_status_accepts_only_the_six_states():
    """pending, in_progress, complete, needs_revision, uncertain, abandoned."""
    from samvad.protocol import TaskStatus

    for status in TaskStatus:
        assert _make(task_status=status.value).task_status == status
    with pytest.raises(INVALID):
        _make(task_status="finished")


def test_performative_accepts_only_the_eight_speech_acts():
    from samvad.protocol import Performative

    for act in Performative:
        # spawn_refused must carry one of the four reasons peers know how to
        # read; every other performative is happy with the default payload.
        extra = {"payload": {"reason": "max_depth"}} if act is Performative.SPAWN_REFUSED else {}
        assert _make(performative=act.value, **extra).performative == act
    with pytest.raises(INVALID):
        _make(performative="please_do_this")


# --- artifacts and claims ---------------------------------------------------


def test_artifact_ref_must_be_sha256():
    """`sha256:` + 64 hex chars. Artifacts travel as refs, never inline bytes."""
    ok = {"ref": sha256_ref(), "kind": "python_source", "summary": "impl", "tokens": 380}
    assert _make(artifacts=[ok]).artifacts[0].ref.startswith("sha256:")

    for bad_ref in ("md5:abc", "sha256:tooshort", sha256_ref()[:-1], "9f2a"):
        with pytest.raises(INVALID):
            _make(artifacts=[{**ok, "ref": bad_ref}])


def test_artifact_summary_is_capped():
    """<= 200 chars. It is what the receiver decides from before paying."""
    with pytest.raises(INVALID):
        _make(
            artifacts=[
                {
                    "ref": sha256_ref(),
                    "kind": "python_source",
                    "summary": "x" * 201,
                    "tokens": 10,
                }
            ]
        )


def test_claim_evidence_type_is_constrained():
    """model_prior is a valid value and counts as UNGROUNDED -- measurement 3."""
    from samvad.protocol import EvidenceType

    claim = {
        "claim": "bisect_left is O(log n)",
        "evidence": "test_bisect.py::test_complexity passed",
        "evidence_type": EvidenceType.TEST_OUTPUT.value,
    }
    assert _make(claims=[claim]).claims[0].evidence_type == EvidenceType.TEST_OUTPUT

    with pytest.raises(INVALID):
        _make(claims=[{**claim, "evidence_type": "vibes"}])
