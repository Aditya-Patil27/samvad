"""Contract: state. Seam between P3 and P1/P2.

P3's job this week is to make this file pass.

The seam to watch is idempotency: **P3 stores** (`MessageLog.seen()`), **P1
decides** (returns the cached response instead of re-dispatching). This file
tests P3's half; tests/contract/test_transport.py tests P1's.
"""
import pytest

from tests.support import envelope, has_message, implemented, now_iso

pytestmark = pytest.mark.skipif(
    not has_message(),
    reason="week 1 joint task: write protocol.py together first",
)


def _make(**overrides):
    from samvad.protocol import Message

    return Message(**envelope(**overrides))


def _blob_store():
    """The real store once it exists; skip until then."""
    from samvad.store.blobs import BlobStore

    store = BlobStore()
    if not implemented(store.put, b"probe", "text"):
        pytest.skip("BlobStore not implemented yet")
    return store


def _message_log():
    from samvad.store.log import MessageLog

    log = MessageLog()
    if not implemented(log.seen, "probe"):
        pytest.skip("MessageLog not implemented yet")
    return log


# --- content addressing -----------------------------------------------------


def test_same_bytes_produce_one_blob_and_same_ref():
    """Dedup is the entire win: partition, do not broadcast.

    Four agents with 200K windows is not an 800K pool if all four hold the
    same tokens.
    """
    store = _blob_store()
    a = store.put(b"def binary_search(xs, t): ...", "python_source")
    b = store.put(b"def binary_search(xs, t): ...", "python_source")
    assert a.ref == b.ref
    assert store.get(a.ref) == b"def binary_search(xs, t): ..."


def test_different_bytes_produce_different_refs():
    store = _blob_store()
    assert store.put(b"one", "text").ref != store.put(b"two", "text").ref


def test_ref_is_sha256_of_the_content():
    """Content-addressed the way git is -- the ref is derivable, not assigned."""
    import hashlib

    store = _blob_store()
    data = b"traceback: ZeroDivisionError"
    assert store.put(data, "traceback").ref == "sha256:" + hashlib.sha256(data).hexdigest()


def test_unknown_ref_returns_none():
    store = _blob_store()
    assert store.get("sha256:" + "0" * 64) is None
    assert store.has("sha256:" + "0" * 64) is False


def test_artifact_ref_carries_the_price_before_the_receiver_pays():
    """`tokens` is what lets a receiver decide whether to hydrate at all."""
    store = _blob_store()
    ref = store.put(b"x" * 4000, "python_source")
    assert ref.tokens > 0
    assert len(ref.summary) <= 200


# --- the message log --------------------------------------------------------


def test_replay_restores_conversation_after_restart():
    """Turns a crash from 'demo over' into 'restart and resume mid-conversation'."""
    log = _message_log()
    sent = [_make(turn=i, lamport=i + 1) for i in range(3)]
    for m in sent:
        log.append(m)

    assert [m.message_id for m in log.replay()] == [m.message_id for m in sent]


def test_replay_order_is_lamport_not_timestamp():
    """Wall time will lie to you, and it will do it in your demo recording.

    Two messages whose timestamps disagree with their Lamport order must come
    back in Lamport order.
    """
    log = _message_log()
    log.append(_make(lamport=9, timestamp=now_iso(-60), sender="agent_a"))
    log.append(_make(lamport=2, timestamp=now_iso(0), sender="agent_b"))

    assert [m.lamport for m in log.replay()] == [2, 9]


def test_seen_returns_none_for_an_unknown_message():
    assert _message_log().seen("never-stored") is None


def test_seen_returns_the_stored_response_for_a_duplicate():
    """P3's half of the idempotency seam. P1 is what acts on this."""
    log = _message_log()
    msg = _make()
    log.append(msg)
    assert log.seen(msg.message_id) is not None


def test_appending_the_same_message_id_twice_does_not_duplicate():
    """message_id is the primary key. At-least-once delivery means retries."""
    log = _message_log()
    msg = _make()
    log.append(msg)
    log.append(msg)
    assert len([m for m in log.replay() if m.message_id == msg.message_id]) == 1


# --- backpressure -----------------------------------------------------------


def test_backpressure_trips_above_ninety_percent():
    """A sender seeing used/limit > 0.9 on its peer sends refs and summaries only."""
    from samvad.store.context import BACKPRESSURE_THRESHOLD, ContextTracker

    tracker = ContextTracker()
    peer_full = _make(context={"used": 190_000, "limit": 200_000}).context
    if not implemented(tracker.should_send_full, peer_full):
        pytest.skip("ContextTracker not implemented yet")

    assert BACKPRESSURE_THRESHOLD == 0.9
    assert tracker.should_send_full(peer_full) is False

    peer_roomy = _make(context={"used": 14_200, "limit": 200_000}).context
    assert tracker.should_send_full(peer_roomy) is True
