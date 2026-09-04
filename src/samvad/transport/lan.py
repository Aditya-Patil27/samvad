# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""HTTP to a peer's LAN address.

Delivery is at-least-once: retry on connection failure, and expect duplicates
at the other end. The receiver's idempotency cache is what makes that safe.

Reuse ONE httpx.AsyncClient -- do not build a new one per request.
"""


class LanTransport:
    async def send(self, msg) -> None:
        raise NotImplementedError
