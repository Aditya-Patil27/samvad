# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""SSE stream for the dashboard.

P4 is a READ-ONLY consumer. This layer never writes to the system -- that
zero coupling is what lets P4 work without blocking anyone.
"""


async def event_stream():
    """Yield SSE frames: message flow, per-node context usage, running USD."""
    raise NotImplementedError
