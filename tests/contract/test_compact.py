"""Contract: hot / warm / cold compaction. P3's store/compact.py.

The DoD line this file closes: a conversation exceeding the hot-tier limit
compacts without losing the `root_task`. Everything below is built from real
`protocol.Message` envelopes, signed the way the wire signs them, because a
compactor that only ever saw dicts would never meet the validators that
`model_copy` skips.
"""
import pytest

from tests.support import envelope, has_message, now_iso, sha256_ref

pytestmark = pytest.mark.skipif(
    not has_message(),
    reason="week 1 joint task: write protocol.py together first",
)

CONVERSATION = "conv-3f8b20"

#: An em dash, a newline and a double space: everything a paraphrase or a
#: whitespace "cleanup" would quietly change.
ROOT_TASK = (
    "Implement a binary search function — O(log n), stdlib only.\n"
    "  Edge cases: [] and [x]."
)

#: Past WARM_PAYLOAD_CHARS in every long result, so a summary that kept the
#: whole body would show it.
TAIL = "# -- tail: well past the warm cutoff --"

IMPL = (
    "def binary_search(xs: list[int], target: int) -> int:\n"
    "    lo, hi = 0, len(xs)\n"
    "    while lo < hi:\n"
    "        mid = (lo + hi) // 2\n"
    "        if xs[mid] < target:\n"
    "            lo = mid + 1\n"
    "        else:\n"
    "            hi = mid\n"
    "    return lo if lo < len(xs) and xs[lo] == target else -1\n"
)

#: Four devices, four clocks. agent_a runs fast and agent_b slow, so every
#: a -> b hop has a reply timestamped before the message it answers.
SKEW = {"agent_a": 40.0, "agent_b": -40.0, "agent_c": 0.0}

COST = {"input": 4200, "output": 830, "cache_read": 0, "usd": 0.0, "model": "mock"}

#: (lamport, turn, sender, receiver, performative, task_status, payload, extra)
#: In (lamport, sender) order. The two workers answer a fan-out concurrently,
#: so they share a turn and a Lamport value and only `sender` orders them.
SCRIPT = [
    (1, 0, "agent_a", "agent_b", "task_request", "pending",
     {"subtask": "implement binary_search over a sorted list",
      "constraints": ["O(log n)", "Python 3.11", "pytest"]}, {}),
    (3, 1, "agent_b", "agent_c", "spawn_request", "in_progress",
     {"role": "executor", "prompt_id": "executor",
      "slice": {"usd_remaining": 0.10, "turns_remaining": 3}}, {}),
    (5, 2, "agent_c", "agent_b", "spawn_refused", "in_progress",
     {"reason": "at_capacity"}, {}),
    (8, 3, "agent_b/worker_1", "agent_b", "child_result", "in_progress",
     {"result": IMPL + "\n# empty input\n" + IMPL + TAIL,
      "summary": "draft impl, empty case"}, {"cost": COST}),
    (8, 3, "agent_b/worker_2", "agent_b", "child_result", "in_progress",
     {"result": IMPL + "\n# single element\n" + IMPL + TAIL,
      "summary": "draft impl, single case"}, {"cost": COST}),
    (10, 4, "agent_b", "agent_a", "task_result", "needs_revision",
     {"result": "pytest -q: 1 failed, 3 passed\n" + IMPL + TAIL, "exit_code": 1},
     {"cost": COST,
      "artifacts": [{"ref": sha256_ref(b"traceback: IndexError"), "kind": "traceback",
                     "summary": "IndexError on xs[mid] for []", "tokens": 41}],
      "claims": [{"claim": "binary_search([], 3) raises IndexError",
                  "evidence": "test_bs.py::test_empty failed",
                  "evidence_type": "test_output"}]}),
    (12, 5, "agent_b/worker_2", "agent_b", "budget_exhausted", "abandoned",
     {"spent": 0.10, "needed": 0.02}, {}),
    (14, 6, "agent_b", "agent_a", "uncertain", "uncertain",
     {"question": "return -1 or raise ValueError when the target is absent?",
      "options": ["-1", "ValueError"]}, {}),
    # --- the hot tier: the last HOT_TURNS by Lamport ----------------------
    (16, 7, "agent_a", "agent_b", "task_request", "in_progress",
     {"subtask": "return -1; fix the empty-input crash",
      "constraints": ["O(log n)", "Python 3.11", "pytest"]}, {}),
    (18, 8, "agent_b", "agent_a", "task_result", "needs_revision",
     {"result": "pytest -q: 1 failed, 3 passed\n" + IMPL + TAIL, "exit_code": 1},
     {"cost": COST}),
    (20, 9, "agent_a", "agent_b", "task_request", "in_progress",
     {"subtask": "use bisect_left; keep the signature",
      "constraints": ["O(log n)", "Python 3.11", "pytest"]}, {}),
    (22, 10, "agent_b", "agent_a", "task_result", "in_progress",
     {"result": "pytest -q: 4 passed\n" + IMPL + TAIL, "exit_code": 0},
     {"cost": COST,
      "artifacts": [{"ref": sha256_ref(IMPL.encode()), "kind": "python_source",
                     "summary": "binary search impl, 9 lines", "tokens": 96}]}),
    (24, 11, "agent_a", "agent_c", "task_request", "in_progress",
     {"subtask": "review the implementation against the root task",
      "constraints": ["O(log n)"]}, {}),
    (26, 12, "agent_c", "agent_a", "task_result", "complete",
     {"result": "approved: 4 passed, matches the root task", "exit_code": 0},
     {"cost": COST,
      "claims": [{"claim": "all four tests pass", "evidence": "pytest -q: 4 passed",
                  "evidence_type": "test_output"}]}),
]


def _conversation(secret):
    """SCRIPT as signed envelopes, each replying to the one before it."""
    from samvad import security
    from samvad.protocol import Message

    history, previous = [], None
    for lamport, turn, sender, receiver, performative, status, payload, extra in SCRIPT:
        msg = Message(**envelope(
            conversation_id=CONVERSATION,
            reply_to=previous,
            turn=turn,
            lamport=lamport,
            timestamp=now_iso(SKEW[sender.split("/", 1)[0]]),
            sender=sender,
            receiver=receiver,
            performative=performative,
            root_task=ROOT_TASK,
            payload=payload,
            task_status=status,
            **extra,
        ))
        msg.sig = security.sign(msg, secret)
        history.append(msg)
        previous = msg.message_id
    return history


@pytest.fixture
def history(secret):
    from samvad.store.compact import HOT_TURNS

    built = _conversation(secret)
    # Without this, raising HOT_TURNS past len(SCRIPT) would turn every test
    # below into a test of the under-the-limit early return.
    assert len(built) > HOT_TURNS
    return built


def _split(history):
    from samvad.store.compact import HOT_TURNS

    return history[:-HOT_TURNS], history[-HOT_TURNS:]


def _by_id(messages):
    return {m.message_id: m for m in messages}


def _snapshot(messages):
    return {m.message_id: m.model_dump_json() for m in messages}


NEVER_COMPACTED = {"uncertain", "spawn_refused", "budget_exhausted"}


# --- at or under the limit --------------------------------------------------


def test_a_conversation_at_or_under_the_hot_limit_is_returned_unchanged(history):
    from samvad.store.compact import HOT_TURNS, Compactor

    compactor = Compactor()
    for size in (0, 1, HOT_TURNS):
        short = history[len(history) - size:]
        compacted = compactor.compact(short)

        assert [m.model_dump_json() for m in compacted] == [m.model_dump_json() for m in short]
        assert compactor.tokens_saved(short, compacted) == 0


# --- what survives ----------------------------------------------------------


def test_every_message_keeps_the_original_root_task_byte_identical(history):
    from samvad.store.compact import Compactor

    compacted = Compactor().compact(history)

    assert len(compacted) == len(history)
    for msg in compacted:
        assert msg.root_task.encode("utf-8") == ROOT_TASK.encode("utf-8")


def test_the_hot_tier_is_returned_verbatim(history):
    from samvad.store.compact import Compactor

    _, hot = _split(history)
    before = _snapshot(hot)
    compacted = _by_id(Compactor().compact(history))

    for message_id, dumped in before.items():
        assert compacted[message_id].model_dump_json() == dumped


def test_older_messages_lose_their_payload_to_a_summary(history):
    from samvad.store.compact import Compactor

    old, _ = _split(history)
    compactor = Compactor()
    compacted = compactor.compact(history)
    after = _by_id(compacted)

    warmed = [m for m in old if m.performative not in NEVER_COMPACTED]
    assert warmed, "the old segment has nothing compactable -- the script is wrong"
    for original in warmed:
        payload = after[original.message_id].payload
        assert set(payload) == {"compacted", "summary"}
        assert payload["compacted"] is True
        assert payload["summary"].startswith(f"{original.performative} from {original.sender}")
        if TAIL in original.payload.get("result", ""):
            assert TAIL not in payload["summary"]

    assert compactor.tokens_saved(history, compacted) > 0


def test_compaction_changes_nothing_but_the_payload(history):
    """root_task, artifacts and claims survive unconditionally, and so does
    everything else: the docstring says what goes is `payload` and only that."""
    from samvad.store.compact import Compactor

    after = _by_id(Compactor().compact(history))

    for original in history:
        assert (
            after[original.message_id].model_dump(exclude={"payload"})
            == original.model_dump(exclude={"payload"})
        )


def test_uncertain_spawn_refused_and_budget_exhausted_are_never_compacted(history):
    from samvad.store.compact import Compactor

    old, _ = _split(history)
    protected = [m for m in old if m.performative in NEVER_COMPACTED]
    assert {str(m.performative) for m in protected} == NEVER_COMPACTED
    before = _snapshot(protected)
    after = _by_id(Compactor().compact(history))

    for message_id, dumped in before.items():
        assert after[message_id].model_dump_json() == dumped


def test_compacted_messages_are_still_valid_envelopes(history):
    """`model_copy(update=...)` skips validation. A compacted history still
    gets serialised and parsed again -- into the log, into a prompt -- so each
    message has to survive the schema it bypassed."""
    from samvad.protocol import Message
    from samvad.store.compact import Compactor

    for msg in Compactor().compact(history):
        assert Message.model_validate_json(msg.model_dump_json()) == msg


# --- signature, identity and order ------------------------------------------


def test_compaction_never_resigns_so_a_summary_cannot_pass_for_the_original(history, secret):
    """The sig is carried through untouched, which is what keeps it honest: it
    still vouches for the payload the sender signed, so a compacted copy fails
    verification instead of passing for a message the sender never sent."""
    from samvad import security
    from samvad.store.compact import Compactor

    old, hot = _split(history)
    after = _by_id(Compactor().compact(history))

    for original in hot:
        assert security.verify(after[original.message_id], secret) is True
    for original in old:
        msg = after[original.message_id]
        assert msg.sig == original.sig
        if original.performative in NEVER_COMPACTED:
            assert security.verify(msg, secret) is True
        else:
            assert security.verify(msg, secret) is False


def test_order_is_lamport_then_sender_whatever_order_the_history_arrives_in(history):
    """Never timestamp: SKEW stamps every agent_b reply before the agent_a
    message it answers. Reversed, the newest turns come first in the list, so
    the hot tier has to be found by Lamport, and worker_2 arrives ahead of
    worker_1 at the same Lamport value, so only `sender` can put them back."""
    from samvad.store.compact import Compactor

    compactor = Compactor()
    from_reversed = compactor.compact(list(reversed(history)))
    from_ordered = compactor.compact(history)

    assert [m.message_id for m in from_reversed] == [m.message_id for m in history]
    assert [m.model_dump_json() for m in from_reversed] == [
        m.model_dump_json() for m in from_ordered
    ]


def test_a_compacted_message_keeps_the_id_its_full_original_is_logged_under(history):
    """Cold is SQLite, retrieved on demand, and message_id is the key it is
    retrieved by. A warm summary that lost its id would be a dead end."""
    from samvad.store.compact import Compactor
    from samvad.store.log import MessageLog

    log = MessageLog()
    for msg in history:
        log.append(msg)
    cold = _by_id(log.conversation(CONVERSATION))
    originals = _by_id(history)

    warmed = [m for m in Compactor().compact(log.conversation(CONVERSATION))
              if m.payload.get("compacted")]
    assert warmed
    for msg in warmed:
        assert cold[msg.message_id].payload == originals[msg.message_id].payload
