# OWNER: P3 -- see docs/WORK.md. Do not edit if you are not P3.
"""SQLite append-only message log.

Two jobs:
  1. replay()  -- rebuild state on boot. Turns a crash from "demo over" into
                  "restart and resume mid-conversation".
  2. seen()    -- backs P1's idempotency. Delivery is at-least-once; a
                  duplicate reaching the handler means a duplicate LLM call,
                  a duplicate charge, and two divergent answers.

SEAM WITH P1: P3 STORES (seen), P1 DECIDES (returns the cached response
instead of dispatching). Agree this out loud on day one or you will both
build half of it.
"""


class MessageLog:
    def append(self, msg) -> None:
        raise NotImplementedError

    def replay(self):
        """-> Iterator[Message], in Lamport order."""
        raise NotImplementedError

    def seen(self, message_id: str):
        """-> the stored response, or None."""
        raise NotImplementedError
