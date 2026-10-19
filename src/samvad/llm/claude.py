# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""Anthropic backend -- the only one that costs money.

Model routing by task class (docs -> Metering):
    planning, code generation      claude-opus-5      $5.00 / $25.00 per MTok
    "is this done?", classifying   claude-haiku-4-5   $1.00 /  $5.00 per MTok

Reached only on purpose: `backend_for` refuses a claude-* name unless
SAMVAD_ALLOW_PAID=1, and `default_backend` never gets that far unless MOCK_LLM=0.
This system spawns recursively, so the failure mode of an accidental route here
is not a wrong answer, it is a bill.

PROMPT CACHING
Caching is a PREFIX match -- any byte change anywhere in the prefix invalidates
everything after it. Render order is tools -> system -> messages. So a chat
prompt's system turns go first, in the `system` param, and carry the one cache
breakpoint; the conversation follows, uncached. This module adds nothing to the
prefix -- no timestamp, no id, no reordering -- so whether it hits is decided by
the caller keeping it frozen: system prompt and root_task first, never mutated;
timestamps, message ids and the peer's latest turn last.

Below a model's minimum the breakpoint is silently ignored: no error, no cache,
cache_read stays zero. The minimum is 512 tokens on Opus 5, 1024 on Sonnet 5 and
4096 on Haiku 4.5 -- so most short Haiku classifier prompts will never cache.
A plain-string prompt gets no breakpoint at all: it is all volatile, and a cache
write costs more than plain input to store something nobody will read.

Verify with usage.cache_read_input_tokens. Persistently zero on a prefix above
the minimum means something in it is changing -- run scripts/cache_check.py.

USAGE IS THE API'S OWN REPORT, NEVER ESTIMATED.
Anthropic's `input_tokens` counts only the uncached remainder, so `Usage.input`
is input_tokens + cache_creation_input_tokens + cache_read_input_tokens: the
whole prompt. ContextTracker needs that (a cache hit still occupies the window)
and usd_for expects it (it bills input - cache_read).

ONE RETRY POLICY. The SDK's own retries are switched off (max_retries=0) and
openai_compat's policy runs instead, so a 429 is not retried twice over at two
different rates against a key that is already rate limited.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import anthropic

from samvad.llm.base import Completion, Usage, render_prompt
from samvad.llm.ollama import _is_chat_prompt
from samvad.llm.openai_compat import (
    BACKOFF_BASE_SECONDS,
    MAX_ATTEMPTS,
    MAX_BACKOFF_SECONDS,
    _retry_delay,
    _snippet,
)

ENV_VAR = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-opus-5"

#: (input, output) USD per million tokens. Must match agent.MODEL_RATES, which
#: is what actually bills -- tests/contract/test_llm_claude.py holds them equal.
#: A model with no row here is refused, not priced at zero.
RATES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

#: Thinking is on by default on Opus 5 and Sonnet 5 and its tokens count against
#: max_tokens, so the free tiers' 2048 would truncate real planning. It is also
#: the most output one call can bill, so it stays bounded.
DEFAULT_MAX_TOKENS = 8192

#: Not streamed, so nothing arrives until the whole completion is done: the read
#: budget has to cover a full max_tokens generation. An unreachable API should
#: still fail fast.
DEFAULT_TIMEOUT = anthropic.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)

#: Stop reasons after which the text is not a whole answer.
_INCOMPLETE = {
    "max_tokens": "was cut off at max_tokens",
    "refusal": "was declined by a safety classifier",
}


class ClaudeError(RuntimeError):
    """Base for every failure of this backend. Catch this one.

    `usage` is set when the API answered and billed for it. A truncated or
    refused completion still cost money, and a budget that only counts the
    successes is not conserved.
    """

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class ClaudeUnavailable(ClaudeError):
    """Unreachable, unauthenticated, refused, or still rate limited after retrying."""


class ClaudeProtocolError(ClaudeError):
    """The API answered, but not with a usable completion.

    Malformed, missing its usage, truncated, or refused. The SDK does not
    validate responses strictly, so this module does -- better a named error
    here than an AttributeError three frames into the agent loop.
    """


class ClaudeBackend:
    """An LLMBackend over the Anthropic Messages API.

    Args:
        model: A model id with a row in RATES_USD_PER_MTOK.
        api_key: Defaults to ANTHROPIC_API_KEY from the environment. Needed only
            when no client is injected.
        timeout: Passed to the SDK client. See DEFAULT_TIMEOUT.
        client: An `anthropic.AsyncAnthropic`, or anything with an async
            `messages.create`, to use instead of one of our own. This is the
            test seam: tests inject one on `httpx2.MockTransport`, so nothing in
            the suite needs a key or touches the network. An injected SDK client
            has its own retries switched off, and `aclose()` leaves it open.
        max_tokens: Output cap per call, thinking included.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        timeout: anthropic.Timeout | float = DEFAULT_TIMEOUT,
        client: Any = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        if model not in RATES_USD_PER_MTOK:
            # usd_for prices an unknown model at zero, and a paid call priced at
            # zero never decrements the budget that terminates recursion.
            raise ClaudeError(
                f"{model!r} has no row in claude.RATES_USD_PER_MTOK (known: "
                f"{sorted(RATES_USD_PER_MTOK)}). Add its published price there and "
                f"in agent.MODEL_RATES before using it."
            )
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._owns_client = client is None
        if isinstance(client, anthropic.AsyncAnthropic):
            client = client.with_options(max_retries=0)
        self._client = client
        self._key = ""

        if client is None:
            key = api_key if api_key is not None else os.environ.get(ENV_VAR, "")
            if not key:
                raise ClaudeUnavailable(
                    f"{ENV_VAR} is not set. Put it in .env (gitignored) -- never in a "
                    f"tracked file. This backend bills per token."
                )
            self._key = key

    @property
    def model_id(self) -> str:
        """Unprefixed, unlike the siblings: a claude-* id is already unambiguous,
        and it is the key both agent.MODEL_RATES and MODEL_LIMITS look up."""
        return self.model

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                api_key=self._key, max_retries=0, timeout=self.timeout
            )
        return self._client

    async def aclose(self) -> None:
        """Close the client we created. An injected one is left alone."""
        if self._client is not None and self._owns_client:
            await self._client.close()
            self._client = None

    async def complete(self, prompt: Any) -> Completion:
        message = _as_message(await self._create(_request(prompt)))
        usage = _usage(message, self.model_id)

        stop_reason = _field(message, "stop_reason")
        if stop_reason in _INCOMPLETE:
            raise ClaudeProtocolError(
                f"the completion {_INCOMPLETE[stop_reason]} (stop_reason={stop_reason})",
                usage=usage,
            )
        return Completion(text=_text_of(message, usage), usage=usage)

    async def _create(self, request: dict[str, Any]) -> Any:
        """Retry 429, 5xx and network failures with backoff; never another 4xx.

        A 401 does not become valid by being sent again, and neither does a
        request the API has rejected as malformed. 429 is the API saying how
        long to wait, in `retry-after`; 529 is it saying it is overloaded.
        """
        last: ClaudeError | None = None
        delay = 0.0

        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                await asyncio.sleep(delay)
            delay = min(BACKOFF_BASE_SECONDS * 2**attempt, MAX_BACKOFF_SECONDS)
            try:
                return await self._get_client().messages.create(
                    model=self.model, max_tokens=self.max_tokens, **request
                )
            except (anthropic.APIResponseValidationError, json.JSONDecodeError) as exc:
                raise ClaudeProtocolError(f"anthropic returned an unreadable body: {exc}") from exc
            except anthropic.APITimeoutError as exc:
                last = ClaudeUnavailable(f"anthropic timed out: {exc}")
            except anthropic.APIConnectionError as exc:
                last = ClaudeUnavailable(f"anthropic unreachable: {exc}")
            except anthropic.APIStatusError as exc:
                status = exc.status_code
                if status == 401:
                    # Not chained: nothing from the rejected request rides along.
                    raise ClaudeUnavailable(
                        f"anthropic rejected the key in {ENV_VAR} (401). If it was ever "
                        f"pasted anywhere shared, rotate it in the Anthropic Console."
                    ) from None
                if status == 429:
                    last = ClaudeUnavailable("anthropic rate limited (429)")
                    delay = _retry_delay(exc.response, attempt)
                elif status < 500:
                    raise ClaudeUnavailable(
                        f"anthropic refused: {status} {_snippet(exc.response.text)}"
                    ) from exc
                else:
                    last = ClaudeUnavailable(f"anthropic server error {status}")

        raise ClaudeUnavailable(f"anthropic failed after {MAX_ATTEMPTS} attempts: {last}")


def _request(prompt: Any) -> dict[str, Any]:
    """The `system` and `messages` params for one prompt.

    The Messages API has no system role, so a chat prompt's system turns are
    lifted into `system` in their original order. The LAST of them carries the
    breakpoint, which caches all of them. Turns are copied as role and content
    only, so nothing else a caller left on them ends up in the prompt.
    """
    if not _is_chat_prompt(prompt):
        return {"messages": [{"role": "user", "content": render_prompt(prompt)}]}

    system = [{"type": "text", "text": t["content"]} for t in prompt if t["role"] == "system"]
    messages = [
        {"role": t["role"], "content": t["content"]} for t in prompt if t["role"] != "system"
    ]
    unknown = sorted({m["role"] for m in messages} - {"user", "assistant"})
    if unknown:
        raise ValueError(f"the Messages API has no {unknown} role; use system, user, assistant")
    if not messages:
        raise ValueError("a chat prompt needs a user turn, not only system text")

    request: dict[str, Any] = {"messages": messages}
    if system:
        system[-1]["cache_control"] = {"type": "ephemeral"}
        request["system"] = system
    return request


def _field(obj: Any, name: str) -> Any:
    """One field of an SDK object or of a plain dict from a fake client."""
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _as_message(result: Any) -> Any:
    """Not strictly validated by the SDK: a non-JSON body comes back as a str
    and a JSON array as a list, rather than as an error."""
    if isinstance(result, (str, bytes)):
        text = result.decode("utf-8", "replace") if isinstance(result, bytes) else result
        raise ClaudeProtocolError(f"anthropic returned a body that is not JSON: {_snippet(text)}")
    if result is None or isinstance(result, (list, tuple, int, float)):
        raise ClaudeProtocolError(f"anthropic returned {type(result).__name__}, not a message")
    return result


def _count(usage: Any, field: str, *, required: bool = False) -> int:
    value = _field(usage, field)
    if value is None and not required:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClaudeProtocolError(f"anthropic returned a bad {field}: {value!r}")
    return value


def _usage(message: Any, model: str) -> Usage:
    """The API's counts, and REQUIRED here, unlike the free backends.

    A free backend that reports nothing still costs nothing. A paid one would
    be priced at zero, and a budget that never decrements never stops a spawn
    tree. The cache fields are optional: absent means none.
    """
    usage = _field(message, "usage")
    if usage is None:
        raise ClaudeProtocolError("anthropic returned no usage, so the call cannot be billed")
    cache_read = _count(usage, "cache_read_input_tokens")
    return Usage(
        input=(
            _count(usage, "input_tokens", required=True)
            + _count(usage, "cache_creation_input_tokens")
            + cache_read
        ),
        output=_count(usage, "output_tokens", required=True),
        cache_read=cache_read,
        model=model,
    )


def _text_of(message: Any, usage: Usage) -> str:
    """The text blocks, joined; thinking and anything else is skipped.

    Thinking is on by default on Opus 5 and Sonnet 5, so the first block is
    often not text -- reading content[0].text is the bug this avoids.
    """
    content = _field(message, "content")
    if not isinstance(content, list):
        raise ClaudeProtocolError(f"anthropic returned no content list: {content!r}", usage=usage)
    texts = [_field(block, "text") for block in content if _field(block, "type") == "text"]
    if not texts or not all(isinstance(text, str) for text in texts):
        kinds = [_field(block, "type") for block in content]
        raise ClaudeProtocolError(f"anthropic returned no usable text block: {kinds}", usage=usage)
    return "".join(texts)
