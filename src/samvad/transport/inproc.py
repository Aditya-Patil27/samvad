# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""asyncio-queue transport for children on the parent's own device.

Ship this FIRST, in week 1, with FakeInbox -- P2, P3 and P4 are all blocked
without it. Your teammates being unblocked matters more than your layer
being finished.

Still signs every message. No shortcuts: see transport/base.py.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

from samvad import security


class InProcTransport:
    """Delivery over an asyncio queue. Same envelope, same signing, no wire.

    The signing is not ceremony. Measurement 1 compares inproc against loopback
    against lan, and the only thing allowed to differ between them is delivery.
    Skipping the HMAC here to make the fast path faster would make it a second
    code path and the comparison would measure that instead of the network.
    """

    def __init__(
        self,
        inbox: Callable[[Any], Awaitable[None]] | None = None,
        secret: str | None = None,
    ) -> None:
        self._inbox = inbox
        self._secret = secret or os.environ.get("SAMVAD_SECRET", "")
        self.queue: asyncio.Queue[Any] = asyncio.Queue()

    async def send(self, msg: Any) -> None:
        """Fire-and-forget. Signs, then hands off without awaiting a handler.

        Returns as soon as the message is queued -- the same contract POST
        /message honours with its 202. A reply arrives later as a new inbound
        message, never as a return value from here.
        """
        msg.sig = security.sign(msg, self._secret)
        await self.queue.put(msg)
        if self._inbox is not None:
            # Scheduled, not awaited: an inbox that calls an LLM takes 5-60s,
            # and send() must not block a sender for that long.
            asyncio.get_running_loop().create_task(self._inbox(msg))

    async def receive(self) -> Any:
        """Next delivered message. Verifies before returning it."""
        msg = await self.queue.get()
        if not security.verify(msg, self._secret):
            raise ValueError(f"rejected unsigned or tampered message {msg.message_id}")
        return msg


class FakeInbox:
    """Records delivered messages. For everyone else's tests."""

    def __init__(self) -> None:
        self.received: list = []

    async def __call__(self, msg) -> None:
        self.received.append(msg)
