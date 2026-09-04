# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""asyncio-queue transport for children on the parent's own device.

Ship this FIRST, in week 1, with FakeInbox -- P2, P3 and P4 are all blocked
without it. Your teammates being unblocked matters more than your layer
being finished.

Still signs every message. No shortcuts: see transport/base.py.
"""


class InProcTransport:
    async def send(self, msg) -> None:
        raise NotImplementedError


class FakeInbox:
    """Records delivered messages. For everyone else's tests."""

    def __init__(self) -> None:
        self.received: list = []

    async def __call__(self, msg) -> None:
        self.received.append(msg)
