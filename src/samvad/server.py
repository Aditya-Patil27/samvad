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
"""
from collections.abc import Awaitable, Callable


def make_app(inbox: Callable[..., Awaitable[None]]):
    """Build the FastAPI app. `inbox` is called in the background, never awaited
    inside the request handler."""
    raise NotImplementedError
