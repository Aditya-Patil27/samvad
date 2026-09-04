"""Two real nodes, two real HTTP servers, signed messages over the wire.

Not a mock and not an in-process shortcut: two uvicorn servers on 127.0.0.1,
each with its own Lamport clock and idempotency cache, talking over HTTP with
HMAC signatures. Swap 127.0.0.1 for a LAN address and this is the LAN demo.
"""
import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

os.environ.setdefault("SAMVAD_SECRET", "roundtrip-demo-secret")

import uvicorn

from samvad import security
from samvad.protocol import Message
from samvad.server import make_app
from samvad.transport.loopback import LoopbackTransport

SECRET = os.environ["SAMVAD_SECRET"]
PEERS = {"agent_a": {"host": "127.0.0.1", "port": 8101},
         "agent_b": {"host": "127.0.0.1", "port": 8102}}

inbox_log: dict[str, list] = {"agent_a": [], "agent_b": []}
handler_calls = {"agent_b": 0}


def envelope(sender, receiver, performative, root_task, conversation_id, **kw):
    return Message(
        protocol_version="2.0", message_id=str(uuid4()), conversation_id=conversation_id,
        turn=kw.pop("turn", 0), lamport=kw.pop("lamport", 1),
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        sender=sender, receiver=receiver, performative=performative,
        root_task=root_task, payload=kw.pop("payload", {}),
        task_status=kw.pop("task_status", "pending"),
        spawn={"parent": sender, "depth": 0, "max_depth": 3},
        budget={"usd_remaining": 0.50, "turns_remaining": 12},
        context={"used": 4200, "limit": 200000}, **kw)


async def main():
    transport = LoopbackTransport(peers=PEERS, secret=SECRET)
    replied = asyncio.Event()

    async def inbox_b(msg):
        """agent_b: receives a task_request, replies with a task_result."""
        handler_calls["agent_b"] += 1
        inbox_log["agent_b"].append(msg)
        clock_b.observe(msg.lamport)
        reply = envelope("agent_b", "agent_a", "task_result", msg.root_task,
                         msg.conversation_id, turn=msg.turn + 1, lamport=clock_b.tick(),
                         task_status="complete",
                         payload={"result": "bisect_left, 12 lines", "exit_code": 0},
                         reply_to=msg.message_id,
                         cost={"input": 4200, "output": 830, "cache_read": 3900,
                               "usd": 0.0031, "model": "claude-opus-5"},
                         claims=[{"claim": "handles the empty list",
                                  "evidence": "test_empty passed", "evidence_type": "test_output"}])
        await transport.send(reply)

    async def inbox_a(msg):
        inbox_log["agent_a"].append(msg)
        clock_a.observe(msg.lamport)
        replied.set()

    from samvad.clock import LamportClock
    clock_a, clock_b = LamportClock(), LamportClock()

    app_a = make_app(inbox_a, agent="agent_a", peers=("agent_a", "agent_b"), secret=SECRET)
    app_b = make_app(inbox_b, agent="agent_b", peers=("agent_a", "agent_b"), secret=SECRET)

    servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
               for app, port in ((app_a, 8101), (app_b, 8102))]
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    while not all(s.started for s in servers):
        await asyncio.sleep(0.05)

    print("two nodes up: agent_a :8101   agent_b :8102\n")

    # --- 1. a -> b -> a, signed, over HTTP -----------------------------
    outbound = envelope("agent_a", "agent_b", "task_request",
                        "Implement a binary search function", "conv-demo-1",
                        lamport=clock_a.tick(),
                        payload={"subtask": "write it", "constraints": ["O(log n)"]})
    await transport.send(outbound)
    await asyncio.wait_for(replied.wait(), timeout=5)

    got_b, got_a = inbox_log["agent_b"][0], inbox_log["agent_a"][0]
    print("1. ROUND TRIP")
    print(f"   a -> b  {got_b.message_id}  lamport {got_b.lamport}  {got_b.performative}")
    print(f"   b -> a  {got_a.message_id}  lamport {got_a.lamport}  {got_a.performative}")
    print(f"   same message_id on both sides: {got_a.reply_to == outbound.message_id}")
    print(f"   lamport strictly increased:    {got_a.lamport > got_b.lamport}")
    print(f"   both signed and verified:      "
          f"{security.verify(got_b, SECRET) and security.verify(got_a, SECRET)}")
    print(f"   root_task copied verbatim:     {got_a.root_task == outbound.root_task}")

    # --- 2. duplicate message_id --------------------------------------
    import httpx
    before = handler_calls["agent_b"]
    body = outbound.model_dump(mode="json")
    async with httpx.AsyncClient() as c:
        r1 = await c.post("http://127.0.0.1:8102/message", json=body)
        r2 = await c.post("http://127.0.0.1:8102/message", json=body)
    print("\n2. IDEMPOTENCY")
    print(f"   first {r1.status_code} {r1.json()}   duplicate {r2.status_code} {r2.json()}")
    print(f"   identical response:      {r1.json() == r2.json()}")
    await asyncio.sleep(0.2)
    print(f"   handler ran extra times: {handler_calls['agent_b'] - before} (must be 0)")

    # --- 3. tampered body ---------------------------------------------
    tampered = envelope("agent_a", "agent_b", "task_request", "Implement a binary search function",
                        "conv-demo-2", lamport=clock_a.tick())
    tampered.sig = security.sign(tampered, SECRET)
    tampered.root_task = "Implement quicksort instead"
    async with httpx.AsyncClient() as c:
        r = await c.post("http://127.0.0.1:8102/message", json=tampered.model_dump(mode="json"))
    print("\n3. TAMPERED BODY")
    print(f"   {r.status_code} {r.json()}  (401 expected)")

    # --- 4. goal drift within a conversation ---------------------------
    drift = envelope("agent_a", "agent_b", "task_request", "Implement a fast search function",
                     "conv-demo-1", turn=4, lamport=clock_a.tick())
    drift.sig = security.sign(drift, SECRET)
    async with httpx.AsyncClient() as c:
        r = await c.post("http://127.0.0.1:8102/message", json=drift.model_dump(mode="json"))
    print("\n4. ROOT_TASK DRIFT")
    print(f"   {r.status_code} {r.json()}  (409 expected)")

    # --- 5. 202 does not await the handler -----------------------------
    import time
    slow_done = asyncio.Event()

    async def slow_inbox(msg):
        await asyncio.sleep(3.0)
        slow_done.set()

    app_slow = make_app(slow_inbox, agent="agent_b", peers=("agent_a", "agent_b"), secret=SECRET)
    s = uvicorn.Server(uvicorn.Config(app_slow, host="127.0.0.1", port=8103, log_level="error"))
    t = asyncio.create_task(s.serve())
    while not s.started:
        await asyncio.sleep(0.05)
    m = envelope("agent_a", "agent_b", "task_request", "slow", "conv-demo-3", lamport=99)
    m.sig = security.sign(m, SECRET)
    async with httpx.AsyncClient() as c:
        t0 = time.perf_counter()
        r = await c.post("http://127.0.0.1:8103/message", json=m.model_dump(mode="json"))
        elapsed = time.perf_counter() - t0
    print("\n5. THE 202 INVARIANT")
    print(f"   handler sleeps 3.0s; POST returned {r.status_code} in {elapsed*1000:.0f} ms")
    print(f"   handler still running:  {not slow_done.is_set()}")

    for srv in [*servers, s]:
        srv.should_exit = True
    await asyncio.gather(*tasks, t, return_exceptions=True)


asyncio.run(main())
