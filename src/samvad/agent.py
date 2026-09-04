# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""The core loop: a message arrived, produce replies.

INVARIANT: handle() RETURNS messages, it never sends them. Transport belongs
to P1. This keeps the agent core pure and testable.

INVARIANT: an agent may not set task_status=COMPLETE when the last
task_result carried exit_code != 0. Enforced HERE, in a code path -- not
requested in a prompt. The runtime is the arbiter; the model is advisory.
This is the single most effective hallucination control in the system.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from samvad.budget import CircuitBreaker, can_afford, slice_budget
from samvad.clock import LamportClock
from samvad.protocol import (
    Budget,
    Claim,
    Cost,
    Message,
    Performative,
    Spawn,
    TaskStatus,
    depth_of,
    parent_of,
)

#: USD per million tokens, (input, output). Unknown models cost nothing, which
#: is true for the mock and for a local Ollama and honest for anything else --
#: a guessed rate would quietly corrupt the cost column in every measurement.
MODEL_RATES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

#: What one call is assumed to cost when deciding whether a branch can afford
#: to spawn. Deliberately pessimistic: refusing a spawn that would have fit is
#: recoverable, allowing one that does not is how a tree overruns a budget.
ESTIMATED_CALL_USD = 0.02


def usd_for(usage: Any) -> float:
    rate_in, rate_out = MODEL_RATES.get(getattr(usage, "model", ""), (0.0, 0.0))
    billable_in = max(0, usage.input - usage.cache_read)
    return (billable_in * rate_in + usage.output * rate_out) / 1_000_000


class Agent:
    """One agent. Pure: messages in, messages out, nothing sent."""

    def __init__(
        self,
        llm: Any = None,
        agent_path: str | None = None,
        clock: LamportClock | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.llm = llm if llm is not None else _default_backend()
        self.agent_path = agent_path
        self.clock = clock or LamportClock()
        self.breaker = breaker or CircuitBreaker()

    # --- the loop ---------------------------------------------------------

    async def handle(self, msg: Message) -> list[Message]:
        """-> zero or more outbound messages."""
        self.clock.observe(msg.lamport)

        if msg.performative is Performative.SPAWN_REQUEST:
            return self._handle_spawn(msg)
        if msg.performative in (Performative.TASK_REQUEST, Performative.TASK_RESULT):
            return await self._handle_task(msg)
        # spawn_ack, child_result, spawn_refused, budget_exhausted, uncertain:
        # terminal for this agent. Silence is a valid reply -- a protocol that
        # requires an answer to every message never stops talking.
        return []

    # --- spawning ---------------------------------------------------------

    def _handle_spawn(self, msg: Message) -> list[Message]:
        """Refusals are protocol states peers reason about, never exceptions.

        Depth is checked before money: a spawn that is too deep is refused for
        being too deep even if it could afford it, because that is the more
        specific and more actionable reason to report.
        """
        child_depth = msg.spawn.depth + 1
        if child_depth > msg.spawn.max_depth:
            return [self._refuse(msg, "max_depth")]

        if not self.breaker.check(ESTIMATED_CALL_USD):
            return [self._refuse(msg, "budget_policy")]

        if not can_afford(msg.budget, ESTIMATED_CALL_USD):
            # Conservation IS the fork-bomb guard: a branch that cannot pay for
            # one call cannot spawn, so recursion terminates on the budget
            # rather than on a separate depth counter nobody maintains.
            return [self._refuse(msg, "budget_policy")]

        n = max(1, int(msg.payload.get("fan_out", 1)))
        slices = slice_budget(msg.budget, n)
        if not all(can_afford(s, ESTIMATED_CALL_USD) for s in slices):
            return [self._refuse(msg, "budget_policy")]

        return [
            self._reply(
                msg,
                Performative.SPAWN_ACK,
                TaskStatus.IN_PROGRESS,
                payload={"agent_path": f"{self._me(msg)}/worker_{i + 1}"},
                budget=slices[i],
            )
            for i in range(n)
        ]

    def _refuse(self, msg: Message, reason: str) -> Message:
        return self._reply(
            msg,
            Performative.SPAWN_REFUSED,
            TaskStatus.ABANDONED if reason != "max_depth" else TaskStatus.NEEDS_REVISION,
            payload={"reason": reason},
        )

    # --- task work --------------------------------------------------------

    async def _handle_task(self, msg: Message) -> list[Message]:
        if not can_afford(msg.budget, ESTIMATED_CALL_USD) or not self.breaker.check(
            ESTIMATED_CALL_USD
        ):
            # Decrement before the call, never after. If the remainder will not
            # cover it, say so and do not call.
            return [
                self._reply(
                    msg,
                    Performative.BUDGET_EXHAUSTED,
                    TaskStatus.ABANDONED,
                    payload={"spent": 0.0, "needed": ESTIMATED_CALL_USD},
                )
            ]

        completion = await self.llm.complete(self._prompt(msg))
        cost_usd = usd_for(completion.usage)
        self.breaker.record(cost_usd)

        advisory = _parse(completion.text)
        status = self._arbitrate(msg, advisory)

        return [
            self._reply(
                msg,
                Performative.TASK_RESULT,
                status,
                payload={
                    "result": advisory.get("result", completion.text),
                    **({"exit_code": advisory["exit_code"]} if "exit_code" in advisory else {}),
                },
                claims=[Claim(**c) for c in advisory.get("claims", []) if isinstance(c, dict)],
                cost=Cost(
                    input=completion.usage.input,
                    output=completion.usage.output,
                    cache_read=completion.usage.cache_read,
                    usd=cost_usd,
                    model=completion.usage.model,
                ),
                budget=Budget(
                    usd_remaining=max(0.0, msg.budget.usd_remaining - cost_usd),
                    turns_remaining=max(0, msg.budget.turns_remaining - 1),
                ),
            )
        ]

    def _arbitrate(self, msg: Message, advisory: dict[str, Any]) -> TaskStatus:
        """THE hallucination control. The runtime decides; the model advises.

        An agent may not report `complete` for work whose exit code was
        non-zero. The model is asked for a status and its answer is taken --
        except here, where a failing exit code overrules it outright. In a
        prompt this is a request the model can ignore; in this function it is
        not. That difference is the entire control.
        """
        exit_code = advisory.get("exit_code", msg.payload.get("exit_code"))
        claimed = advisory.get("task_status")
        status = TaskStatus(claimed) if claimed in set(TaskStatus) else TaskStatus.COMPLETE

        if status is TaskStatus.COMPLETE and exit_code not in (None, 0):
            return TaskStatus.NEEDS_REVISION
        return status

    def _prompt(self, msg: Message) -> dict[str, Any]:
        """root_task goes in verbatim, every turn.

        Never paraphrased and never summarised: by turn six, an agent that only
        ever saw a paraphrase is solving a different problem, and nothing in
        the transcript will say when it changed.
        """
        return {
            "root_task": msg.root_task,
            "performative": str(msg.performative),
            "payload": msg.payload,
            "artifacts": [a.model_dump() for a in msg.artifacts],
            "turn": msg.turn,
        }

    # --- envelope construction -------------------------------------------

    def _me(self, msg: Message) -> str:
        """This agent's own path. It is whoever the message was addressed to."""
        return self.agent_path or msg.receiver

    def _reply(
        self,
        msg: Message,
        performative: Performative,
        status: TaskStatus,
        *,
        payload: dict[str, Any] | None = None,
        claims: list[Claim] | None = None,
        cost: Cost | None = None,
        budget: Budget | None = None,
    ) -> Message:
        me = self._me(msg)
        return Message(
            protocol_version=msg.protocol_version,
            message_id=str(uuid4()),
            conversation_id=msg.conversation_id,
            reply_to=msg.message_id,
            turn=msg.turn + 1,
            lamport=self.clock.tick(),
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            sender=me,
            receiver=msg.sender,
            performative=performative,
            root_task=msg.root_task,
            payload=payload or {},
            task_status=status,
            claims=claims or [],
            spawn=Spawn(
                parent=parent_of(me), depth=depth_of(me), max_depth=msg.spawn.max_depth
            ),
            budget=budget or msg.budget,
            context=msg.context,
            cost=cost,
        )


def _parse(text: str) -> dict[str, Any]:
    """Read the model's reply as JSON, tolerating that it is not.

    Validate at boundaries: an LLM response is untrusted input. A model that
    answers in prose instead of JSON is a normal Tuesday, and it must degrade
    to "the text is the result" rather than raise inside the loop.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _default_backend() -> Any:
    """MOCK_LLM=1 is the default, not the exception.

    All development runs on the mock. Live API calls cost real money on
    someone's personal key, and this system spawns recursively -- so the
    expensive backend is the one you have to ask for by name.
    """
    if os.environ.get("MOCK_LLM", "1") != "0":
        from samvad.llm.mock import MockBackend

        return MockBackend()
    from samvad.llm.claude import ClaudeBackend

    return ClaudeBackend()
