# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""MOCK_LLM=1 -- canned responses, zero cost.

Build this SECOND, right after the interface. All transport, routing, spawn
topology and failure-mode work runs against it. You will iterate on the wire
hundreds of times; none of it should cost anything.

Doubles as the demo-day fallback.

Three properties this has to hold, because every other layer leans on them:

- **No network, no cost, no sleeps.** There is not a single I/O call below.
- **Deterministic.** The same prompt gives the same text and the same token
  counts, on every machine, every run. A mock that varies makes four people
  debug a flaky suite instead of the protocol.
- **It never hangs and never exhausts.** Queued replies are consumed in order
  and the last one repeats forever. A fake that raises on the reply someone
  forgot to queue turns a transport bug hunt into a fake-LLM bug hunt.

`Completion` always carries `Usage` -- input, output, cache_read and a model id
-- because every task_result reports its own cost and the dashboard sums those
live across the mesh. A mock that returned bare text would leave measurement 4
with nothing to measure.
"""
from __future__ import annotations

import json
from typing import Any

from samvad.llm.base import Completion, Usage

DEFAULT_MODEL = "mock"
DEFAULT_REPLY = "ok"

#: Chars per token. Not a real tokenizer -- deliberately. Token counts here are
#: for exercising the accounting path, and P3's ContextTracker must use the
#: real count (docs/PROTOCOL.md is explicit that an estimate makes backpressure
#: worthless).
_CHARS_PER_TOKEN = 4


def _prompt_text(prompt: Any) -> str:
    """A stable string for any prompt shape.

    P2 has not settled the prompt type yet and the mock must not be the thing
    that forces the decision. Stable is the requirement: a default `repr()`
    carries a memory address, which would make token counts differ between runs.
    """
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, bytes):
        return prompt.decode("utf-8", "replace")
    try:
        return json.dumps(prompt, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return repr(prompt)


def _tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


class MockBackend:
    """An LLMBackend that answers from a queue instead of an API.

    Args:
        replies: Answers to hand back, in order. The last one repeats once
            the queue is drained, so a caller can never exhaust it -- the same
            rule tests/fakes.py::StubLLM follows. An empty queue answers
            ``"ok"``.
        model: Model id stamped on every Usage. Set it to distinguish families
            in the log -- measurement 5 is about uncorrelated errors, and two
            mock peers reporting the same id prove nothing.
        simulate_cache: When true (the default), a prompt this instance has
            already seen reports its input tokens as ``cache_read``, the way a
            real prefix-cache hit does. Switch it off for a test that wants
            ``cache_read == 0`` unconditionally.
    """

    def __init__(
        self,
        replies: list[str] | None = None,
        model: str = DEFAULT_MODEL,
        simulate_cache: bool = True,
    ) -> None:
        self.replies: list[str] = list(replies) if replies else []
        self.model = model
        self.simulate_cache = simulate_cache
        self.prompts: list[Any] = []
        self._seen: set[str] = set()

    def queue(self, *replies: str) -> None:
        """Add replies to the back of the queue."""
        self.replies.extend(replies)

    @property
    def call_count(self) -> int:
        return len(self.prompts)

    async def complete(self, prompt: Any) -> Completion:
        self.prompts.append(prompt)

        if not self.replies:
            text = DEFAULT_REPLY
        elif len(self.replies) > 1:
            text = self.replies.pop(0)
        else:
            text = self.replies[0]

        rendered = _prompt_text(prompt)
        input_tokens = _tokens(rendered)
        cache_read = input_tokens if self.simulate_cache and rendered in self._seen else 0
        self._seen.add(rendered)

        return Completion(
            text=text,
            usage=Usage(
                input=input_tokens,
                output=_tokens(text),
                cache_read=cache_read,
                model=self.model,
            ),
        )
