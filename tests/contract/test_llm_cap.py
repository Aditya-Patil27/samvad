"""Contract: at most eight LLM calls in flight per node.

Rate limits arrive well before CPU limits. A fan-out of twenty against a
hosted free tier is twenty simultaneous requests, and the provider answers the
ninth with a 429. The cap turns that burst into a queue: the ninth call WAITS
for a slot. It is not refused and it does not crash, because a refusal here
would be a protocol state nobody asked for, about a limit that clears itself
in seconds.

Three things are defended:

1. **Eight, exactly.** Twenty concurrent tasks peak at eight calls in flight,
   and all twenty still finish.
2. **Money before a slot.** A branch that cannot afford the call is answered
   `budget_exhausted` at once. It never queues behind eight slow calls only to
   be told no, and it never calls.
3. **Per node, and still 202.** Every message a node receives -- its own and
   its children's -- shares the one cap, and the queue sits in the background
   handler, so `POST /message` still answers 202 while calls wait.

Every test here runs under MOCK_LLM=1 with a stub. No live API, ever.
"""
from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.fakes import StubLLM
from tests.support import envelope, has_message

pytestmark = pytest.mark.skipif(
    not has_message(),
    reason="week 1 joint task: write protocol.py together first",
)

#: Comfortably more than the cap, so the queue is unmistakable.
BURST = 20


class GatedLLM(StubLLM):
    """A StubLLM that holds every call open until `release` is set.

    Counts the calls in flight at once and the highest that count ever
    reached, which is the number the cap is about.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.in_flight = 0
        self.peak = 0

    async def complete(self, prompt: Any) -> Any:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.release.wait()
            return await super().complete(prompt)
        finally:
            self.in_flight -= 1


def _task(**overrides: Any):
    from samvad.protocol import Message

    return Message(**envelope(**overrides))


async def _settle() -> None:
    """Let every scheduled task run until it blocks. No wall clock involved."""
    for _ in range(20):
        await asyncio.sleep(0)


# --- eight, exactly ---------------------------------------------------------


async def test_the_ninth_llm_call_waits_for_a_slot_and_every_call_finishes():
    from samvad.agent import Agent
    from samvad.protocol import Performative
    from samvad.supervisor import MAX_CONCURRENCY

    llm = GatedLLM()
    agent = Agent(llm=llm, agent_path="agent_b")
    tasks = [asyncio.create_task(agent.handle(_task())) for _ in range(BURST)]
    await _settle()

    assert llm.in_flight == MAX_CONCURRENCY
    assert not any(t.done() for t in tasks), (
        "a call over the cap was answered instead of waiting for a slot"
    )

    llm.release.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

    assert llm.peak == MAX_CONCURRENCY
    assert llm.call_count == BURST
    assert all(r[0].performative is Performative.TASK_RESULT for r in results)


# --- money before a slot ----------------------------------------------------


async def test_a_branch_that_cannot_afford_the_call_never_queues_for_a_slot():
    """Every slot is held. A broke branch must still be answered at once --
    if it reached for a slot first, it would block here until the timeout."""
    from samvad.agent import Agent
    from samvad.protocol import Performative
    from samvad.supervisor import MAX_CONCURRENCY

    llm = GatedLLM()
    agent = Agent(llm=llm, agent_path="agent_b")
    holders = [asyncio.create_task(agent.handle(_task())) for _ in range(MAX_CONCURRENCY)]
    await _settle()
    assert llm.in_flight == MAX_CONCURRENCY

    broke = _task(budget={"usd_remaining": 0.0, "turns_remaining": 0})
    try:
        replies = await asyncio.wait_for(agent.handle(broke), timeout=1.0)
    except TimeoutError:
        pytest.fail("an unaffordable task queued for an LLM slot instead of refusing at once")

    assert [r.performative for r in replies] == [Performative.BUDGET_EXHAUSTED]

    llm.release.set()
    await asyncio.wait_for(asyncio.gather(*holders), timeout=5.0)
    assert llm.call_count == MAX_CONCURRENCY, "the unaffordable branch called the LLM"


# --- per node, and still 202 ------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _node(tmp_path: Path):
    from samvad.node import Node

    config = tmp_path / "peers.yaml"
    config.write_text(yaml.safe_dump({
        "peers": {
            name: {"host": "127.0.0.1", "port": _free_port(), "role": "executor",
                   "model": "mock"}
            for name in ("agent_a", "agent_b")
        },
    }), encoding="utf-8")
    return Node("agent_a", config=str(config))


class _Outbox:
    """Stands in for the node's transport: records replies, delivers nothing."""

    def __init__(self, expected: int) -> None:
        self.sent: list[Any] = []
        self.expected = expected
        self.all_sent = asyncio.Event()

    async def send(self, msg: Any) -> None:
        self.sent.append(msg)
        if len(self.sent) >= self.expected:
            self.all_sent.set()


async def test_one_node_shares_one_cap_and_post_message_still_returns_202(tmp_path, secret):
    """Half the burst is addressed to the node, half to its children. They are
    all handled on this node, so they all count against its one cap -- and the
    queue lives in the background handler, never in the request."""
    import httpx

    from samvad import security
    from samvad.supervisor import MAX_CONCURRENCY

    node = _node(tmp_path)
    llm = GatedLLM()
    node.agent.llm = llm
    node.transport = outbox = _Outbox(expected=BURST)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=node.app), base_url="http://testserver"
    ) as client:
        for i in range(BURST):
            msg = _task(sender="agent_b",
                        receiver="agent_a" if i % 2 else f"agent_a/worker_{i}")
            msg.sig = security.sign(msg, secret)

            started = time.perf_counter()
            response = await client.post("/message", json=msg.model_dump(mode="json"))
            elapsed = time.perf_counter() - started

            assert response.status_code == 202
            assert elapsed < 0.5, f"POST blocked for {elapsed:.2f}s behind the LLM cap"

    await _settle()
    assert llm.in_flight == MAX_CONCURRENCY

    llm.release.set()
    await asyncio.wait_for(outbox.all_sent.wait(), timeout=5.0)
    assert llm.peak == MAX_CONCURRENCY
    assert llm.call_count == BURST
