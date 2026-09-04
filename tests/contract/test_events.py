"""Contract: the SSE seam between the node and dashboard/index.html. Owner P4.

Deliberately NOT gated on protocol.py. `samvad.events` must never import the
envelope -- it takes duck-typed input (a mapping, or anything with the right
attributes) so the dashboard can be driven before the week-1 protocol session
and unchanged after it.

The consumer is the spec. Every assertion here is traceable to a line in
dashboard/index.html: the header comment documents the two frame shapes,
`onNode` and `onMessage` say which fields are read, and `EventSource` fixes
the wire format at `data: <json>` followed by a blank line.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from samvad import events

# Exactly the keys dashboard/index.html reads. A frame carrying anything else
# is dead weight on the wire; a frame missing one of these silently degrades.
NODE_KEYS = {"type", "agent", "model", "up", "context", "children", "budget_usd"}
MESSAGE_KEYS = {"type", "lamport", "sender", "receiver", "performative",
                "task_status", "cost", "child", "note", "dashed"}


class FakeMessage:
    """An envelope-shaped object that is NOT protocol.Message.

    The point of the duck typing: this test passes before protocol.py exists.
    """

    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


def envelope_dict(**overrides: object) -> dict[str, object]:
    msg: dict[str, object] = {
        "lamport": 14,
        "sender": "agent_a",
        "receiver": "agent_b",
        "performative": "task_request",
        "task_status": "in_progress",
        "cost": {"input": 4200, "output": 830, "cache_read": 3900,
                 "usd": 0.0031, "model": "claude-opus-5"},
    }
    msg.update(overrides)
    return msg


def data_of(frame: str) -> dict:
    return json.loads(frame[len("data: "):].strip())


# ---- the module must stand alone -------------------------------------------

def test_events_does_not_import_the_frozen_protocol():
    """P4 ships before protocol.py lands. An import here would couple the
    dashboard to the week-1 joint session and break this whole file."""
    source = inspect.getsource(events)
    assert "samvad.protocol" not in source
    assert "from samvad import protocol" not in source


# ---- wire format ------------------------------------------------------------

def test_sse_frame_is_data_colon_json_blank_line():
    """EventSource parses `data: <json>` terminated by a BLANK line. Get either
    wrong and the browser receives nothing at all, silently."""
    frame = events.sse({"type": "node", "agent": "agent_a"})
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert frame.count("\n") == 2, "one data line, then the terminating blank line"
    assert data_of(frame) == {"type": "node", "agent": "agent_a"}


def test_sse_never_emits_a_raw_newline_inside_the_payload():
    """A newline inside a value would split one frame into two and desync the
    stream. JSON escaping is what prevents it -- assert it, do not assume it."""
    frame = events.sse({"type": "message", "note": "line one\nline two"})
    assert frame.count("\n") == 2
    assert data_of(frame)["note"] == "line one\nline two"


# ---- message frames ---------------------------------------------------------

def test_message_frame_matches_the_documented_shape():
    frame = events.message_frame(envelope_dict())
    assert frame == {
        "type": "message",
        "lamport": 14,
        "sender": "agent_a",
        "receiver": "agent_b",
        "performative": "task_request",
        "task_status": "in_progress",
        "cost": {"usd": 0.0031},
    }
    assert set(frame) <= MESSAGE_KEYS


def test_message_frame_accepts_an_object_as_well_as_a_mapping():
    """Duck-typed both ways: a dict off the log, or an envelope object."""
    from_object = events.message_frame(FakeMessage(**envelope_dict()))
    assert from_object == events.message_frame(envelope_dict())


def test_message_frame_carries_only_usd_from_cost():
    """`onMessage` reads m.cost?.usd and nothing else. Token counts on the wire
    would be four unused numbers per message."""
    frame = events.message_frame(envelope_dict())
    assert frame["cost"] == {"usd": 0.0031}


def test_message_frame_reads_cost_from_an_object_too():
    cost = FakeMessage(usd=0.5, input=1, output=2)
    assert events.message_frame(envelope_dict(cost=cost))["cost"] == {"usd": 0.5}


def test_message_frame_omits_absent_optionals():
    """`m.cost?.usd || 0` tolerates a missing cost. Sending nulls is noise."""
    frame = events.message_frame(envelope_dict(cost=None, task_status=None))
    assert "cost" not in frame
    assert "task_status" not in frame
    assert frame["performative"] == "task_request"


def test_message_frame_surfaces_the_child_path_on_a_spawn_ack():
    """`onMessage` draws an on-device child branch from m.child. The envelope
    carries it as payload.agent_path -- no invented field."""
    frame = events.message_frame(envelope_dict(
        performative="spawn_ack",
        receiver="agent_b",
        payload={"agent_path": "agent_b/worker_1"},
    ))
    assert frame["child"] == "agent_b/worker_1"


def test_message_frame_has_no_child_when_the_payload_has_no_path():
    assert "child" not in events.message_frame(envelope_dict(payload={"subtask": "x"}))


def test_message_frame_keeps_the_full_agent_path():
    """`peerOf` splits on '/' in the dashboard, so the column is derived there.
    Truncating here would throw away which child sent it."""
    frame = events.message_frame(envelope_dict(sender="agent_b/worker_2"))
    assert frame["sender"] == "agent_b/worker_2"


def test_message_frame_lamport_is_an_int():
    """Ordering is by Lamport clock. `Math.max` on a string silently misorders."""
    assert events.message_frame(envelope_dict(lamport="14"))["lamport"] == 14


# ---- node frames ------------------------------------------------------------

def test_node_frame_matches_the_documented_shape():
    frame = events.node_frame(
        "agent_b", model="claude-opus-5", up=True,
        context={"used": 14200, "limit": 200000}, children=2, budget_usd=0.42,
    )
    assert frame == {
        "type": "node", "agent": "agent_b", "model": "claude-opus-5", "up": True,
        "context": {"used": 14200, "limit": 200000}, "children": 2, "budget_usd": 0.42,
    }
    assert set(frame) <= NODE_KEYS


def test_node_frame_omits_unset_fields_so_partial_updates_merge():
    """`onNode` does `n.model ?? s.model` -- an absent key keeps the previous
    value, a null would blank the strip. Partial updates must omit, not null."""
    frame = events.node_frame("agent_b", up=False)
    assert frame == {"type": "node", "agent": "agent_b", "up": False}


def test_node_frame_down_is_representable():
    """The kill-a-peer demo depends on up:false reaching the dashboard."""
    assert events.node_frame("agent_c", up=False)["up"] is False


# ---- publish / subscribe ----------------------------------------------------

async def test_stream_yields_published_frames_in_order():
    bus = events.EventBus()
    agen = events.event_stream(bus, keepalive=None).__aiter__()
    bus.publish_node("agent_a", model="claude-opus-5", up=True)
    bus.publish_message(envelope_dict())

    first = data_of(await asyncio.wait_for(anext(agen), 1))
    second = data_of(await asyncio.wait_for(anext(agen), 1))
    assert first["type"] == "node" and first["agent"] == "agent_a"
    assert second["type"] == "message" and second["lamport"] == 14
    await agen.aclose()


async def test_every_connected_dashboard_gets_every_frame():
    bus = events.EventBus()
    a = events.event_stream(bus, keepalive=None).__aiter__()
    b = events.event_stream(bus, keepalive=None).__aiter__()
    bus.publish_message(envelope_dict(lamport=7))

    for agen in (a, b):
        assert data_of(await asyncio.wait_for(anext(agen), 1))["lamport"] == 7
    await a.aclose()
    await b.aclose()


async def test_a_new_dashboard_is_seeded_with_the_latest_node_state():
    """A dashboard that connects mid-run would otherwise show four empty
    columns until the next node frame happened to arrive."""
    bus = events.EventBus()
    bus.publish_node("agent_a", model="claude-opus-5", up=True)
    bus.publish_message(envelope_dict())        # not replayed -- history is P3's job
    bus.publish_node("agent_a", up=False)

    agen = events.event_stream(bus, keepalive=None).__aiter__()
    seed = data_of(await asyncio.wait_for(anext(agen), 1))
    assert seed == {"type": "node", "agent": "agent_a",
                    "model": "claude-opus-5", "up": False}
    await agen.aclose()


def test_publish_is_synchronous_and_never_blocks_the_producer():
    """The node calls this from its hot path. If it could block or await, the
    dashboard would be able to slow the system down -- P4 must not do that."""
    assert not inspect.iscoroutinefunction(events.EventBus.publish)
    assert not inspect.iscoroutinefunction(events.EventBus.publish_message)
    assert not inspect.iscoroutinefunction(events.EventBus.publish_node)


async def test_a_stalled_dashboard_drops_frames_instead_of_blocking():
    """A browser that stops reading must lose old frames, not apply
    backpressure to the node. Newest wins -- a live view of stale data is
    worse than a gap."""
    bus = events.EventBus(buffer=4)
    agen = events.event_stream(bus, keepalive=None).__aiter__()
    for n in range(20):                      # nobody is consuming
        bus.publish_message(envelope_dict(lamport=n))

    seen = [data_of(await asyncio.wait_for(anext(agen), 1))["lamport"] for _ in range(4)]
    assert seen == [16, 17, 18, 19], "oldest dropped, newest kept"
    await agen.aclose()


async def test_a_disconnected_dashboard_is_forgotten():
    """Closing the generator is how Starlette signals a dropped client. Leaking
    the queue would grow the node's memory for the length of the run."""
    bus = events.EventBus()
    agen = events.event_stream(bus, keepalive=None).__aiter__()
    bus.publish_message(envelope_dict())
    await asyncio.wait_for(anext(agen), 1)
    assert bus.subscribers == 1
    await agen.aclose()

    assert bus.subscribers == 0
    bus.publish_message(envelope_dict())      # must not raise with nobody attached


async def test_keepalive_emits_a_comment_not_a_data_frame():
    """A proxy or a laptop lid can kill an idle connection. The comment keeps
    it warm; EventSource ignores it, so it can never be mistaken for a frame."""
    bus = events.EventBus()
    agen = events.event_stream(bus, keepalive=0.01).__aiter__()
    beat = await asyncio.wait_for(anext(agen), 1)
    assert beat.startswith(":")
    assert beat.endswith("\n\n")
    assert not beat.startswith("data:")
    await agen.aclose()


async def test_event_stream_defaults_to_the_module_bus():
    """What P1 mounts: `StreamingResponse(event_stream(), ...)` with no wiring."""
    agen = events.event_stream(keepalive=None).__aiter__()
    events.BUS.publish_message(envelope_dict(lamport=99))
    frame = data_of(await asyncio.wait_for(anext(agen), 1))
    assert frame["lamport"] == 99
    await agen.aclose()


def test_frames_are_json_serialisable_as_emitted():
    """Anything the node hands us crosses the wire as JSON or not at all."""
    for frame in (events.node_frame("agent_a", up=True), events.message_frame(envelope_dict())):
        assert json.loads(json.dumps(frame)) == frame


def test_message_frame_rejects_an_envelope_with_no_sender():
    """Validate at the boundary: `PEERS.indexOf(undefined)` is -1 and the row
    is dropped without a word. Fail here instead, where it is visible."""
    with pytest.raises(ValueError):
        events.message_frame(envelope_dict(sender=None))
