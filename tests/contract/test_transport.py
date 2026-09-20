"""Contract: the wire. Seam between P1 and everyone.

P1's job this week is to make this file pass.

The 202 test is the important one -- it is the invariant that makes the whole
architecture asynchronous rather than four blocked HTTP clients. An LLM call
takes 5-60s; a synchronous reply would leave the caller blocked until it timed
out, and would make "async" true only inside a process rather than on the wire.

Address resolution lives in test_routing.py: it needs no envelope, so it is not
gated behind this file's skip.
"""
import asyncio
import time

import pytest

from tests.fakes import RecordingInbox
from tests.support import envelope, has_message, implemented

pytestmark = pytest.mark.skipif(
    not has_message(),
    reason="week 1 joint task: write protocol.py together first",
)


def _make(**overrides):
    from samvad.protocol import Message

    return Message(**envelope(**overrides))


def _app(inbox):
    from samvad.server import make_app

    if not implemented(make_app, inbox):
        pytest.skip("server.make_app not implemented yet")
    return make_app(inbox)


async def _client(app):
    """ASGI client that never touches a real socket."""
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def _signed(secret, **overrides):
    from samvad import security

    msg = _make(**overrides)
    msg.sig = security.sign(msg, secret)
    return msg


# --- the 202 invariant ------------------------------------------------------


async def test_post_message_returns_202_without_awaiting_handler(secret):
    """POST /message returns 202 immediately and NEVER awaits an LLM call.

    The handler here sleeps for 3s. The response must come back long before
    that, and the handler must still be unfinished when it does.
    """
    finished = asyncio.Event()

    async def slow_inbox(msg):
        await asyncio.sleep(3.0)
        finished.set()

    app = _app(slow_inbox)
    msg = _signed(secret)

    async with await _client(app) as client:
        started = time.perf_counter()
        response = await client.post("/message", json=msg.model_dump(mode="json"))
        elapsed = time.perf_counter() - started

    assert response.status_code == 202
    assert response.json() == {"accepted": msg.message_id}
    assert elapsed < 0.5, f"POST blocked for {elapsed:.2f}s -- it awaited the handler"
    assert not finished.is_set(), "the handler completed before the response returned"


async def test_health_reports_agent_lamport_and_children():
    app = _app(RecordingInbox())
    async with await _client(app) as client:
        body = (await client.get("/health")).json()
    for key in ("agent", "lamport", "children"):
        assert key in body


async def test_peers_returns_the_peer_table():
    app = _app(RecordingInbox())
    async with await _client(app) as client:
        response = await client.get("/peers")
    assert response.status_code == 200


# --- idempotency: P1's half of the seam -------------------------------------


async def test_duplicate_message_id_returns_cached_and_does_not_redispatch(secret):
    """A duplicate reaching the handler means a duplicate LLM call, a duplicate
    charge, and two divergent answers to the same question.

    P3 stores (`MessageLog.seen()`); P1 decides. This tests P1's decision.
    """
    inbox = RecordingInbox()
    app = _app(inbox)
    body = _signed(secret).model_dump(mode="json")

    async with await _client(app) as client:
        first = await client.post("/message", json=body)
        second = await client.post("/message", json=body)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json() == first.json(), "the duplicate must return the cached response"
    await asyncio.sleep(0.1)
    assert inbox.calls == 1, f"handler ran {inbox.calls} times -- the duplicate re-dispatched"


# --- rejection paths --------------------------------------------------------


async def test_bad_signature_returns_401(secret):
    """Rejected loudly and logged. Never best-effort parsed."""
    inbox = RecordingInbox()
    app = _app(inbox)
    msg = _signed(secret)
    msg.root_task = "Implement quicksort instead"

    async with await _client(app) as client:
        response = await client.post("/message", json=msg.model_dump(mode="json"))

    assert response.status_code == 401
    assert inbox.calls == 0, "a message with a bad signature reached the handler"


async def test_version_mismatch_returns_400():
    """Do not coerce, do not best-effort parse. A loud rejection is the point."""
    inbox = RecordingInbox()
    app = _app(inbox)

    async with await _client(app) as client:
        response = await client.post("/message", json=envelope(protocol_version="1.0"))

    assert response.status_code == 400
    assert inbox.calls == 0


async def test_unknown_sender_returns_403(secret):
    """A sender absent from the peer table is refused, not silently accepted."""
    inbox = RecordingInbox()
    app = _app(inbox)

    async with await _client(app) as client:
        response = await client.post(
            "/message",
            json=_signed(
                secret,
                sender="agent_z",
                spawn={"parent": "agent_z", "depth": 0, "max_depth": 3},
            ).model_dump(mode="json"),
        )

    assert response.status_code == 403
    assert inbox.calls == 0


async def test_root_task_change_within_conversation_is_rejected(secret):
    """root_task is immutable for the whole conversation, copied verbatim.

    By turn six, agents that only see paraphrases are solving a different
    problem. Goal drift is hallucination's quiet cousin, and the receiver is
    where it has to be caught -- the model cannot police itself here.
    """
    inbox = RecordingInbox()
    app = _app(inbox)
    first = _signed(secret, turn=0, root_task="Implement a binary search function")
    drifted = _signed(
        secret,
        conversation_id=first.conversation_id,
        turn=1,
        root_task="Implement a fast search function",
    )

    async with await _client(app) as client:
        accepted = await client.post("/message", json=first.model_dump(mode="json"))
        assert accepted.status_code == 202
        response = await client.post("/message", json=drifted.model_dump(mode="json"))

    assert response.status_code >= 400, "root_task drifted within a conversation and was accepted"


# --- transport equivalence --------------------------------------------------


async def test_inproc_still_signs_every_message(secret):
    """No unsigned fast path. An exception here would be a second code path
    and would invalidate measurement 1."""
    from samvad import security
    from samvad.transport.inproc import InProcTransport

    transport = InProcTransport()
    msg = _make()
    if not implemented(transport.send, msg):
        pytest.skip("InProcTransport not implemented yet")

    delivered = await transport.send(msg) or msg
    assert delivered.sig is not None, "inproc delivered an unsigned message"
    assert security.verify(delivered, secret) is True


def test_same_envelope_over_all_three_transports():
    """The envelope is identical across inproc / loopback / lan. Only delivery
    differs -- that is the only reason measurement 1 means anything.

    Asserted on the canonical bytes, since that is what signing covers and what
    would silently diverge if a transport reserialised differently.
    """
    from samvad import security

    msg = _make()
    if not implemented(security.canonical, msg):
        pytest.skip("security.canonical not implemented yet")

    assert security.canonical(msg) == security.canonical(
        type(msg).model_validate_json(msg.model_dump_json())
    )


# --- the dashboard is served from the node ----------------------------------

async def test_root_serves_the_dashboard_so_events_is_same_origin():
    """dashboard/index.html calls `EventSource("/events")` -- a relative URL.

    Opened as a file:// URL that resolves to nothing, the page falls back to its
    synthetic run and labels itself "not measured data". During a demo that
    reads as the system being faked rather than the page being opened wrong, so
    the node serves the console itself and the fallback stays a fallback.
    """
    app = _app(RecordingInbox())
    client = await _client(app)
    async with client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'EventSource("/events")' in response.text


async def test_a_missing_dashboard_is_a_404_and_does_not_take_the_node_down():
    """A checkout without dashboard/ is still a working node. Losing the inbox
    because a static file is absent would be the wrong trade."""
    from samvad import server

    app = _app(RecordingInbox())
    client = await _client(app)
    original = server.DASHBOARD_HTML
    server.DASHBOARD_HTML = original.parent / "does-not-exist.html"
    try:
        async with client:
            response = await client.get("/")
            health = await client.get("/health")
    finally:
        server.DASHBOARD_HTML = original

    assert response.status_code == 404
    assert health.status_code == 200, "a missing dashboard must not affect the inbox"
