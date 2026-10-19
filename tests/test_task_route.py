"""POST /task: the dashboard's way to open a conversation.

Localhost only, validated, scheduled and never awaited (the 202 invariant).
"""
import asyncio

import httpx

from samvad.server import MAX_TASK_CHARS, make_app


async def _noop(msg) -> None:
    return None


def _client(app, host: str = "127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 5000)),
        base_url="http://testserver",
    )


def _app(sent: list):
    async def submit(task: str, to: str) -> None:
        sent.append((task, to))

    return make_app(_noop, agent="agent_a", submit_task=submit)


def test_accepts_and_schedules_the_task():
    async def go():
        sent: list = []
        async with _client(_app(sent)) as c:
            r = await c.post("/task", json={"task": "  parse a log line  "})
            await asyncio.sleep(0)
        assert r.status_code == 202
        assert r.json() == {"accepted": "parse a log line", "to": "agent_b"}
        assert sent == [("parse a log line", "agent_b")]

    asyncio.run(go())


def test_rejects_bad_input_without_sending():
    async def go():
        sent: list = []
        async with _client(_app(sent)) as c:
            for body in ({}, {"task": ""}, {"task": "   "}, {"task": 5},
                         {"task": "x" * (MAX_TASK_CHARS + 1)},
                         {"task": "hi", "to": "agent_a"},      # itself
                         {"task": "hi", "to": "nobody"}):
                assert (await c.post("/task", json=body)).status_code == 400, body
            await asyncio.sleep(0)
        assert sent == []

    asyncio.run(go())


def test_refuses_remote_clients():
    async def go():
        sent: list = []
        async with _client(_app(sent), host="192.168.1.9") as c:
            r = await c.post("/task", json={"task": "hi"})
            await asyncio.sleep(0)
        assert r.status_code == 403
        assert sent == []

    asyncio.run(go())


def test_404_when_the_node_takes_no_tasks():
    async def go():
        async with _client(make_app(_noop, agent="agent_a")) as c:
            assert (await c.post("/task", json={"task": "hi"})).status_code == 404

    asyncio.run(go())
