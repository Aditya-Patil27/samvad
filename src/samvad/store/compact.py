# OWNER: P3 -- see docs/WORK.md. Do not edit if you are not P3.
"""Hot / warm / cold tiering.

    hot    last N turns, verbatim
    warm   older turns, summarised
    cold   SQLite, retrieved on demand

Each agent compacts independently -- no coordination needed.

root_task NEVER compacts. It is immutable for the whole conversation and is
re-injected verbatim into every prompt. By turn six, agents that only see
paraphrases are solving a different problem.
"""
from __future__ import annotations

from samvad.protocol import Message, Performative

#: Turns kept verbatim. Small on purpose -- the recent exchange is what the
#: model is actually reasoning about; everything older is reference material
#: that a summary and a ref serve just as well for far fewer tokens.
HOT_TURNS = 6

#: A warm message keeps its shape and loses its body.
WARM_PAYLOAD_CHARS = 200


class Compactor:
    """Shrinks a conversation without losing what makes it that conversation."""

    def __init__(self, hot_turns: int = HOT_TURNS) -> None:
        self.hot_turns = hot_turns

    def compact(self, history: list[Message]) -> list[Message]:
        """Hot turns verbatim, older ones summarised, root_task untouched.

        Three things survive compaction unconditionally, because losing any of
        them changes what the conversation IS rather than merely shortening it:

          root_task   immutable for the conversation and re-injected every
                      turn. An agent working from a compacted paraphrase is
                      solving a different problem and nothing says when it
                      changed.
          artifacts   already refs, not bytes -- they cost almost nothing to
                      keep and are how the dropped content is recoverable.
                      Compacting a ref away turns warm data into lost data.
          claims      the grounding record. Dropping them would make an agent
                      look better evidenced than it was, and measurement 3
                      counts exactly these.

        What goes is `payload`, which is the bulk and the only part that is
        genuinely re-derivable from the artifacts it references.
        """
        if len(history) <= self.hot_turns:
            return list(history)

        ordered = sorted(history, key=lambda m: (m.lamport, m.sender))
        cutoff = len(ordered) - self.hot_turns
        return [
            (self._warm(m) if i < cutoff and self._may_compact(m) else m)
            for i, m in enumerate(ordered)
        ]

    def _may_compact(self, msg: Message) -> bool:
        """Some messages are never worth compacting.

        `uncertain` is a question waiting on a human and `spawn_refused` is why
        a whole branch does not exist -- both are small, and both are exactly
        what someone reads the log to find.
        """
        return msg.performative not in (
            Performative.UNCERTAIN,
            Performative.SPAWN_REFUSED,
            Performative.BUDGET_EXHAUSTED,
        )

    def _warm(self, msg: Message) -> Message:
        summary = self._summarise(msg)
        return msg.model_copy(
            update={
                "payload": {"compacted": True, "summary": summary},
                # root_task, artifacts and claims deliberately carried through
                # untouched -- see compact().
            }
        )

    def _summarise(self, msg: Message) -> str:
        parts = [f"{msg.performative} from {msg.sender}"]
        for key in ("result", "subtask", "reason", "question"):
            if key in msg.payload:
                parts.append(f"{key}: {str(msg.payload[key])[:WARM_PAYLOAD_CHARS]}")
                break
        if msg.artifacts:
            parts.append(f"{len(msg.artifacts)} artifact(s) by ref")
        return " -- ".join(parts)

    def tokens_saved(self, before: list[Message], after: list[Message]) -> int:
        """Rough, and only for the dashboard. Measurement 4 uses reported
        token counts, never this."""
        def size(ms: list[Message]) -> int:
            return sum(len(m.model_dump_json()) for m in ms)

        return max(0, (size(before) - size(after)) // 4)
