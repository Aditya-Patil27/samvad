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
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from samvad.protocol import Message

SCHEMA = Path(__file__).with_name("schema.sql")


class MessageLog:
    """Append-only. Ordered by Lamport, never by wall clock."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = sqlite3.connect(str(path))
        self._db.executescript(SCHEMA.read_text(encoding="utf-8"))

    def append(self, msg: Message, response: dict[str, Any] | None = None) -> None:
        """Record a message and the response it was answered with.

        INSERT OR IGNORE on the message_id primary key. Delivery is
        at-least-once, so the same message arriving twice is expected traffic,
        not corruption -- and the second copy must not overwrite the response
        already returned for the first, or the idempotency guarantee is only
        as good as the last retry.
        """
        self._db.execute(
            "INSERT OR IGNORE INTO messages (message_id, conversation_id, reply_to, turn, "
            "lamport, sender, receiver, performative, task_status, body, response_id, "
            "received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg.message_id, msg.conversation_id, msg.reply_to, msg.turn, msg.lamport,
                msg.sender, msg.receiver, str(msg.performative), str(msg.task_status),
                msg.model_dump_json(),
                (response or {"accepted": msg.message_id}).get("accepted", msg.message_id),
                datetime.now(UTC).isoformat(),
            ),
        )
        self._db.commit()

    def replay(self) -> Iterator[Message]:
        """-> Iterator[Message], in Lamport order.

        ORDER BY (lamport, sender), never by timestamp. Four devices means four
        wall clocks that disagree, and a log sorted by timestamp will show a
        reply arriving before the message it answers -- in your own demo
        recording. `sender` breaks ties deterministically so two nodes
        replaying the same log agree on the order.
        """
        for (body,) in self._db.execute(
            "SELECT body FROM messages ORDER BY lamport, sender"
        ):
            yield Message.model_validate_json(body)

    def seen(self, message_id: str) -> dict[str, Any] | None:
        """-> the stored response, or None.

        P3's half of the idempotency seam. P1 decides what to do with it:
        return this instead of dispatching.
        """
        row = self._db.execute(
            "SELECT response_id FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return {"accepted": row[0]} if row else None

    def root_task_for(self, conversation_id: str) -> str | None:
        """The root_task this conversation was opened with.

        Immutable for the whole conversation, so the first one recorded is the
        only correct answer -- ordered by Lamport for the same reason replay is.
        """
        row = self._db.execute(
            "SELECT body FROM messages WHERE conversation_id = ? ORDER BY lamport, sender LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return Message.model_validate_json(row[0]).root_task if row else None

    def conversation(self, conversation_id: str) -> list[Message]:
        return [
            Message.model_validate_json(body)
            for (body,) in self._db.execute(
                "SELECT body FROM messages WHERE conversation_id = ? ORDER BY lamport, sender",
                (conversation_id,),
            )
        ]

    def record_cost(self, msg: Message) -> None:
        """Costs live in their own table so the dashboard can sum spend
        without deserialising every envelope in the log."""
        if msg.cost is None:
            return
        self._db.execute(
            "INSERT OR IGNORE INTO costs (message_id, input, output, cache_read, usd, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg.message_id, msg.cost.input, msg.cost.output, msg.cost.cache_read,
             msg.cost.usd, msg.cost.model),
        )
        self._db.commit()

    def total_usd(self) -> float:
        return self._db.execute("SELECT COALESCE(SUM(usd), 0.0) FROM costs").fetchone()[0]

    def close(self) -> None:
        self._db.close()
