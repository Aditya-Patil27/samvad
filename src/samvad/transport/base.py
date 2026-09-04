# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""Transport interface. Same envelope over every wire -- only delivery differs.

Measurement 1 compares inproc / loopback / lan. That only works if signing
and serialisation are identical across all three.
"""
from typing import Protocol


class Transport(Protocol):
    async def send(self, msg) -> None:
        """Fire-and-forget. Returns as soon as the peer accepts (202)."""
        ...
