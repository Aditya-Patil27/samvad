# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""HTTP to a peer's LAN address.

Delivery is at-least-once: retry on connection failure, and expect duplicates
at the other end. The receiver's idempotency cache is what makes that safe.

Reuse ONE httpx.AsyncClient -- do not build a new one per request.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

from samvad import security
from samvad.routing import resolve

#: Connect fast -- a peer that is off must not stall the sender. Read is short
#: too: the receiver returns 202 without touching an LLM, so a slow response
#: means the network, not the work.
DEFAULT_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=5.0)

#: At-least-once. Retries are expected and safe because the receiver dedupes on
#: message_id -- that pairing is the whole reason idempotency exists.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 0.25


class LanTransport:
    """Signed HTTP delivery to a peer over the network.

    Every message is signed here, exactly as inproc signs it. Measurement 1
    compares the three transports and only delivery is allowed to differ, so
    there is no cheaper path for the local cases.
    """

    def __init__(
        self,
        peers: dict[str, dict[str, Any]],
        secret: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.peers = peers
        self._secret = secret if secret is not None else os.environ.get("SAMVAD_SECRET", "")
        # ONE client for the process: it pools connections, and building one per
        # request would pay TCP and TLS setup on every hop and skew measurement 1
        # into measuring httpx rather than the network.
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        return self._client

    def url_for(self, address: str) -> str:
        """Longest-prefix route to the owning peer, then its /message route.

        A message to `agent_b/worker_2` goes to agent_b, which forwards. Only
        peers are in the table; children never are.
        """
        peer = resolve(address, self.peers)
        return f"http://{peer['host']}:{peer['port']}/message"

    async def send(self, msg: Any) -> None:
        """Fire-and-forget. Returns as soon as the peer accepts (202)."""
        msg.sig = security.sign(msg, self._secret)
        url = self.url_for(msg.receiver)
        body = msg.model_dump(mode="json")

        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self.client.post(url, json=body)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last = exc
            else:
                if response.status_code == 202:
                    return
                # 4xx is the receiver's considered judgment -- a bad signature
                # or a stale version will not become valid by being sent again.
                # Only retry what could plausibly be transient.
                if response.status_code < 500:
                    raise DeliveryRefused(
                        f"{msg.receiver} refused {msg.message_id}: "
                        f"{response.status_code} {response.text[:120]}"
                    )
                last = DeliveryRefused(f"{response.status_code} from {msg.receiver}")

            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(BACKOFF_SECONDS * (2**attempt))

        raise DeliveryFailed(f"{msg.receiver} unreachable after {MAX_ATTEMPTS} attempts: {last}")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


class DeliveryRefused(RuntimeError):
    """The peer answered and said no. Retrying will not change its mind."""


class DeliveryFailed(RuntimeError):
    """The peer never answered. This is the loss measurement 1 exists to count."""
