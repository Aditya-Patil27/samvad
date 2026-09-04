# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""HTTP to 127.0.0.1 -- several agent processes on one laptop."""


class LoopbackTransport:
    async def send(self, msg) -> None:
        raise NotImplementedError
