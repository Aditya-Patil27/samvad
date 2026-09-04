# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""SSE stream for the dashboard.

P4 is a READ-ONLY consumer. This layer never writes to the system -- that
zero coupling is what lets P4 work without blocking anyone. Nothing here
sends a message, mutates an envelope, or calls into another layer; it takes
what it is handed and turns it into frames.

The consumer is the spec: `dashboard/index.html` already parses these frames.
Its header comment documents the two shapes, and `onNode` / `onMessage` fix
which fields are read:

    data: {"type":"node","agent":"agent_b","model":"claude-opus-5","up":true,
           "context":{"used":14200,"limit":200000},"children":2,"budget_usd":0.42}

    data: {"type":"message","lamport":14,"sender":"agent_a","receiver":"agent_b",
           "performative":"task_request","task_status":"in_progress",
           "cost":{"usd":0.0031}}

Input is duck-typed on purpose -- a plain mapping off P3's log, or anything
with the matching attributes. This module does NOT import the envelope, so it
works before the week-1 protocol session and unchanged after it.

Mounting (P1 owns the route; this is all it needs):

    from samvad.events import BUS, event_stream
    @app.get("/events")
    async def events():
        return StreamingResponse(event_stream(), media_type="text/event-stream")

and the node publishes with `BUS.publish_message(msg)` / `BUS.publish_node(...)`.
Both are synchronous and never block: a stalled browser drops frames rather
than applying backpressure to the node.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

__all__ = ["BUS", "EventBus", "event_stream", "message_frame", "node_frame", "sse"]

BUFFER = 256
"""Frames held per connected dashboard before the oldest are dropped."""

KEEPALIVE_SECONDS = 15.0
"""Idle gap after which a comment is sent so proxies keep the socket open."""


# ---- field access -----------------------------------------------------------

_MISSING = object()


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read `name` off a mapping or an object. The whole duck-typing story."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        value = obj.get(name, _MISSING)
    else:
        value = getattr(obj, name, _MISSING)
    return default if value is _MISSING else value


def _required(obj: Any, name: str) -> Any:
    """Validate at the boundary. A missing sender reaches the dashboard as
    `PEERS.indexOf(undefined)` -> -1 and the row vanishes without a word."""
    value = _field(obj, name)
    if value is None or value == "":
        raise ValueError(f"message has no {name}; cannot render it")
    return value


# ---- frames -----------------------------------------------------------------

def message_frame(msg: Any) -> dict[str, Any]:
    """One envelope -> the `message` frame `onMessage` parses.

    Only what the dashboard reads crosses the wire. Costs are reduced to
    `usd`; the token counts are P3's business, not the view's.
    """
    frame: dict[str, Any] = {
        "type": "message",
        "lamport": int(_required(msg, "lamport")),
        "sender": str(_required(msg, "sender")),
        "receiver": str(_required(msg, "receiver")),
        "performative": str(_required(msg, "performative")),
    }

    status = _field(msg, "task_status")
    if status is not None:
        frame["task_status"] = str(status)

    usd = _field(_field(msg, "cost"), "usd")
    if usd is not None:
        frame["cost"] = {"usd": float(usd)}

    # The dashboard draws an on-device branch from `m.child`. `spawn_ack`
    # carries the path as payload.agent_path -- read, never invented.
    child = _field(_field(msg, "payload"), "agent_path")
    if child:
        frame["child"] = str(child)

    return frame


def node_frame(
    agent: str,
    *,
    model: str | None = None,
    up: bool | None = None,
    context: Any = None,
    children: int | None = None,
    budget_usd: float | None = None,
) -> dict[str, Any]:
    """Per-node state -> the `node` frame `onNode` parses.

    Unset fields are OMITTED, not nulled: `onNode` merges with `??`, so an
    absent key keeps the previous value while a null would blank the strip.
    That is what makes a partial update (say, just `up=False`) safe.
    """
    if not agent:
        raise ValueError("node frame has no agent")

    frame: dict[str, Any] = {"type": "node", "agent": str(agent)}
    if model is not None:
        frame["model"] = str(model)
    if up is not None:
        frame["up"] = bool(up)
    if children is not None:
        frame["children"] = int(children)
    if budget_usd is not None:
        frame["budget_usd"] = float(budget_usd)

    if context is not None:
        used, limit = _field(context, "used"), _field(context, "limit")
        window = {}
        if used is not None:
            window["used"] = int(used)
        if limit is not None:
            window["limit"] = int(limit)
        if window:
            frame["context"] = window

    return frame


def sse(frame: Mapping[str, Any]) -> str:
    """Serialise one frame to the wire format `EventSource` expects.

    `data: <json>` then a BLANK line. Get either wrong -- a missing blank
    line, a raw newline inside the payload -- and the browser receives
    nothing at all, silently. JSON escaping is what guarantees one line.
    """
    return f"data: {json.dumps(frame, separators=(',', ':'))}\n\n"


# ---- fan-out ----------------------------------------------------------------

class EventBus:
    """Fans frames out to every connected dashboard.

    Publishing is synchronous and cannot block: each subscriber owns a bounded
    queue, and a client that stops reading loses its oldest frames. A live view
    of stale data is worse than a gap, and observability must never be able to
    slow the system it observes.
    """

    def __init__(self, buffer: int = BUFFER) -> None:
        self._buffer = buffer
        self._queues: list[asyncio.Queue[dict[str, Any]]] = []
        # Latest state per node, so a dashboard connecting mid-run is not
        # staring at four empty columns until the next node frame happens.
        self._nodes: dict[str, dict[str, Any]] = {}

    @property
    def subscribers(self) -> int:
        return len(self._queues)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Attach a client, seeded with the current node snapshot."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._buffer)
        self._queues.append(queue)
        for snapshot in self._nodes.values():
            self._offer(queue, dict(snapshot))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Detach a client. Idempotent -- a dropped connection may unwind twice."""
        if queue in self._queues:
            self._queues.remove(queue)

    def publish(self, frame: Mapping[str, Any]) -> None:
        """Hand one already-built frame to every subscriber. Never blocks."""
        frame = dict(frame)
        if frame.get("type") == "node" and frame.get("agent"):
            self._nodes.setdefault(str(frame["agent"]), {}).update(frame)
        for queue in list(self._queues):
            self._offer(queue, frame)

    def publish_message(self, msg: Any) -> None:
        """Publish an envelope-shaped mapping or object as a `message` frame."""
        self.publish(message_frame(msg))

    def publish_node(self, agent: str, **state: Any) -> None:
        """Publish per-node state. Pass only what changed; see `node_frame`."""
        self.publish(node_frame(agent, **state))

    def _offer(self, queue: asyncio.Queue[dict[str, Any]], frame: dict[str, Any]) -> None:
        while True:
            try:
                queue.put_nowait(frame)
                return
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()      # drop the oldest, keep the newest
                except asyncio.QueueEmpty:  # pragma: no cover -- single loop
                    return


BUS = EventBus()
"""Process-wide bus. `event_stream()` reads it when no bus is passed."""


def event_stream(
    bus: EventBus | None = None,
    *,
    keepalive: float | None = KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    """Yield SSE frames: message flow, per-node context usage, running USD.

    One call per connected dashboard. Mount it as a streaming response; the
    generator unwinds when the client disconnects and detaches itself, so a
    dropped browser leaves nothing behind.

    Subscribing here rather than on first iteration means a frame published
    between mounting the response and the server's first pull is queued, not
    lost.
    """
    bus = BUS if bus is None else bus
    return _drain(bus, bus.subscribe(), keepalive)


async def _drain(
    bus: EventBus,
    queue: asyncio.Queue[dict[str, Any]],
    keepalive: float | None,
) -> AsyncIterator[str]:
    try:
        while True:
            if keepalive is None:
                yield sse(await queue.get())
                continue
            try:
                frame = await asyncio.wait_for(queue.get(), keepalive)
            except TimeoutError:
                yield ": keepalive\n\n"     # a comment; EventSource ignores it
            else:
                yield sse(frame)
    finally:
        bus.unsubscribe(queue)
