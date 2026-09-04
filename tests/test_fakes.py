"""The fallbacks are insurance, so they get tested too.

These do NOT skip. tests/fakes.py is what ships in the demo if a layer never
lands, and a fallback that quietly fails to hold the property it stands in for
is worse than no fallback at all -- it would let the demo pass while proving
nothing.
"""
from tests.fakes import DictBlobStore, RecordingInbox, StubLLM


def test_dict_blob_store_honours_content_addressing():
    """If P3's layer never arrives, the demo must still dedup."""
    store = DictBlobStore()
    a = store.put(b"same bytes", "text")
    b = store.put(b"same bytes", "text")
    assert a.ref == b.ref
    assert a.ref.startswith("sha256:")
    assert store.has(a.ref)
    assert store.get(a.ref) == b"same bytes"


def test_dict_blob_store_returns_none_for_unknown_ref():
    assert DictBlobStore().get("sha256:" + "0" * 64) is None


def test_dict_blob_store_summary_stays_within_the_protocol_cap():
    ref = DictBlobStore().put(b"x" * 100_000, "python_source")
    assert len(ref.summary) <= 200
    assert ref.tokens > 0


async def test_recording_inbox_counts_calls():
    """The idempotency test depends on this counting correctly."""
    inbox = RecordingInbox()
    await inbox({"message_id": "a"})
    await inbox({"message_id": "b"})
    assert inbox.calls == 2
    assert inbox.ids() == ["a", "b"]


async def test_stub_llm_is_deterministic_and_never_exhausts():
    """A test must never hang waiting on a reply it forgot to queue."""
    llm = StubLLM(replies=["first", "second"])
    assert (await llm.complete("p")).text == "first"
    assert (await llm.complete("p")).text == "second"
    assert (await llm.complete("p")).text == "second"
    assert llm.call_count == 3


async def test_stub_llm_reports_usage():
    """Completion MUST carry usage -- every task_result reports its own cost."""
    usage = (await StubLLM().complete("p")).usage
    assert usage.input > 0
    assert usage.model == "mock"
