# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""Child lifecycle: spawn, track, reparent, cap concurrency.

Failure behaviour (docs/PROTOCOL.md):
    child crashes            retry once, then needs_revision upward
    parent dies mid-fan-out  orphans reparent to the GRANDPARENT
    host peer dies           parent re-places the child, unspent slice intact
    max_depth exceeded       spawn_refused; parent does the work itself

Killing a parent mid-fan-out and watching orphans still deliver is the
primary live demo. Build it so that actually works.
"""

MAX_CONCURRENCY = 8   # rate limits arrive well before CPU limits


class Supervisor:
    async def spawn(self, spec, slice) -> str:
        """-> agent path, e.g. 'agent_a/worker_3'."""
        raise NotImplementedError

    async def on_child_result(self, msg) -> None:
        raise NotImplementedError

    async def reparent(self, orphans: list[str], to: str) -> None:
        raise NotImplementedError
