# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""FastAPI inbox.

INVARIANT: POST /message returns 202 immediately and NEVER awaits an LLM call.
An LLM call takes 5-60s; a synchronous reply would leave the caller blocked
until it timed out. Replies arrive later as new inbound messages.

Routes:
    POST /message      -> 202 {"accepted": message_id}
    GET  /health       -> {"agent", "lamport", "children", "budget_usd"}
    GET  /peers        -> the peer table as this node sees it
    GET  /events       -> SSE stream                    (contributed by P4)
    GET  /blob/{hash}  -> artifact bytes                (contributed by P3)
    GET  /setup        -> peer table + this host's own addresses
    POST /setup        -> validate and write config/peers.yaml  (LOCALHOST ONLY)
    GET  /netcheck     -> probe every peer: reachable / refused / timeout
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError

from samvad import config as peer_config
from samvad import security
from samvad.clock import LamportClock
from samvad.protocol import PROTOCOL_VERSION, Message
from samvad.store.blobs import BlobStore
from samvad.store.log import MessageLog

DEFAULT_PEERS = ("agent_a", "agent_b", "agent_c", "agent_d")


def make_app(
    inbox: Callable[..., Awaitable[None]],
    *,
    agent: str | None = None,
    peers: tuple[str, ...] = DEFAULT_PEERS,
    secret: str | None = None,
    log: MessageLog | None = None,
    blobs: BlobStore | None = None,
) -> FastAPI:
    """Build the FastAPI app. `inbox` is called in the background, never awaited
    inside the request handler."""
    app = FastAPI(title="samvad")
    app.state.agent = agent or os.environ.get("SAMVAD_AGENT", "agent_a")
    app.state.secret = secret if secret is not None else os.environ.get("SAMVAD_SECRET", "")
    app.state.peers = tuple(peers)
    app.state.clock = LamportClock()
    app.state.log = log or MessageLog()
    app.state.blobs = blobs or BlobStore()
    app.state.children = 0

    @app.post("/message")
    async def post_message(request: Request) -> JSONResponse:
        body = await request.json()

        # 400 before anything else. A version mismatch is rejected, never
        # coerced and never best-effort parsed -- silent schema drift is the
        # most likely way this project fails, so the rejection is loud.
        if not isinstance(body, dict) or body.get("protocol_version") != PROTOCOL_VERSION:
            return JSONResponse({"error": "protocol_version"}, status_code=400)

        try:
            msg = Message(**body)
        except ValidationError as exc:
            return JSONResponse({"error": "schema", "detail": exc.errors()[:3]}, status_code=400)

        if msg.peer not in app.state.peers:
            return JSONResponse({"error": "unknown sender", "sender": msg.sender}, status_code=403)

        if not security.verify(msg, app.state.secret):
            return JSONResponse({"error": "signature"}, status_code=401)

        # THE SEAM: P3 stores (MessageLog.seen), P1 decides. Checked BEFORE
        # dispatch -- delivery is at-least-once and retries are expected, and a
        # duplicate reaching the handler means a duplicate LLM call, a duplicate
        # charge, and two divergent answers to one question.
        cached = app.state.log.seen(msg.message_id)
        if cached is not None:
            return JSONResponse(cached, status_code=202)

        # root_task is immutable within a conversation. By turn six, agents that
        # only see paraphrases are solving a different problem, and the receiver
        # is the only place that can catch the drift.
        established = app.state.log.root_task_for(msg.conversation_id)
        if established is not None and established != msg.root_task:
            return JSONResponse(
                {"error": "root_task changed within conversation"}, status_code=409
            )

        app.state.clock.observe(msg.lamport)
        response = {"accepted": msg.message_id}
        app.state.log.append(msg, response)
        app.state.log.record_cost(msg)

        # THE INVARIANT. Scheduled, never awaited: the handler may sit on an LLM
        # for a minute, and this must return in milliseconds.
        asyncio.get_running_loop().create_task(inbox(msg))
        return JSONResponse(response, status_code=202)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "agent": app.state.agent,
            "lamport": app.state.clock.value,
            "children": app.state.children,
            "budget_usd": app.state.log.total_usd(),
        }

    @app.get("/peers")
    async def peer_table() -> dict[str, Any]:
        return {"peers": list(app.state.peers), "agent": app.state.agent}

    @app.get("/blob/{digest}")
    async def get_blob(digest: str) -> Response:
        """Artifact bytes, fetched only if the receiver decides it needs them.

        Contributed by P3, mounted here. This route is the other half of
        "artifacts travel as refs": the ref and its token count ride on the
        message, and the bytes cross the wire only when someone chooses to pay
        for them. An artifact nobody fetches never enters a second context
        window, which is the whole dedup mechanism.
        """
        ref = digest if digest.startswith("sha256:") else f"sha256:{digest}"
        data = app.state.blobs.get(ref)
        if data is None:
            return JSONResponse({"error": "unknown blob", "ref": ref}, status_code=404)
        return Response(content=data, media_type="application/octet-stream")

    @app.get("/setup")
    async def get_setup() -> dict[str, Any]:
        """Current peer table plus this machine's own LAN addresses.

        The address you are least likely to get right is your own, and it is
        the one the node can simply tell you.
        """
        saved = peer_config.read_peers()
        return {
            "peers": (saved or {}).get("peers") or {
                name: {"host": "", "port": 8000 + i, "role": "executor", "model": "mock"}
                for i, name in enumerate(app.state.peers)
            },
            "defaults": (saved or {}).get("defaults")
            or {"max_depth": 3, "budget": {"usd": 0.50, "turns": 12}},
            "local_addresses": peer_config.local_addresses(),
            "config_path": str(peer_config.PEERS_PATH),
            "saved": saved is not None,
        }

    @app.post("/setup")
    async def post_setup(request: Request) -> JSONResponse:
        """Validate a peer table and write config/peers.yaml.

        LOCALHOST ONLY, and that restriction is the point. This endpoint writes
        a file and the node binds 0.0.0.0, so without it anyone on the same
        wifi could rewrite where this node sends its traffic -- on shared
        campus wifi that is a real reach, not a theoretical one. Configuring
        your own machine from your own browser is the whole use case, so the
        restriction costs nothing.

        The path is fixed. No filename comes from the request, so there is
        nothing for a traversal to traverse.
        """
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1", "localhost"):
            return JSONResponse(
                {"error": "config can only be written from this machine",
                 "client": client}, status_code=403)

        body = await request.json()
        try:
            peers = peer_config.validate_peers(body.get("peers"))
        except peer_config.ConfigError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        path = peer_config.write_peers(peers, body.get("defaults"))
        return JSONResponse({
            "written": str(path),
            "peers": peers,
            "yaml": peer_config.to_yaml(peers, body.get("defaults")),
            "note": "copy this file to every device -- it must be byte-identical",
        })

    @app.get("/netcheck")
    async def netcheck() -> dict[str, Any]:
        """Probe every peer. refused means the path works; timeout means it does not.

        The distinction is the whole diagnostic: REFUSED is a reachable host
        with nothing listening yet, which is normal before a peer starts.
        TIMEOUT on shared wifi is almost always AP/client isolation, and no
        amount of code fixes that -- you need a different network.
        """
        saved = peer_config.read_peers() or {}
        table = saved.get("peers") or {}
        results = {}
        for name, cfg in table.items():
            host, port = cfg.get("host"), int(cfg.get("port", 8000))
            if not host:
                results[name] = {"status": "unset", "host": "", "port": port}
                continue
            results[name] = {
                "status": await peer_config.probe(host, port),
                "host": host, "port": port,
                "self": name == app.state.agent,
            }
        return {"peers": results, "checked": len(results)}

    @app.get("/events")
    async def events() -> StreamingResponse:
        """SSE stream for the dashboard. Contributed by P4, mounted here."""
        from samvad.events import event_stream

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app
