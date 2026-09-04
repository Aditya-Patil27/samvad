# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""Local Ollama backend -- the offline peer.

Not just a cost saving. A different model family gives UNCORRELATED errors,
which is the whole basis of measurement 5. Two instances of one model
agreeing is worth almost nothing; they share training priors and wave through
exactly the mistakes they would have made themselves.

`config/peers.example.yaml` gives agent_c `ollama:qwen2.5-coder`. Without this
module there is no `hetero` condition and measurement 5 cannot run.

Three things this has to get right:

- **Usage on every completion.** Ollama reports `prompt_eval_count` and
  `eval_count`; they map to `input` and `output`. `cache_read` is zero, always,
  because Ollama exposes no prompt cache and reports no reuse count -- see
  `_usage` for why a guess would be worse than a zero.
- **Zero USD.** Local inference is free. That is a real asymmetry against
  `claude.py`, and measurement 5 leans on it: the two conditions are compared
  at matched *token* cost precisely because the dollars do not match.
- **Legible failure.** Ollama not running is the common case on three of the
  four laptops. It fails fast with `OllamaUnavailable`, never a hang and never
  an AttributeError surfacing deep in the agent loop.

Errors that peers reason about are protocol states, not exceptions -- but this
layer has no envelope to put a state in. So the two failure modes are raised as
typed exceptions under one base, `OllamaError`, for `agent.py` to catch at the
call site and turn into `uncertain` / a failed `task_result`. The rule is kept
where the envelope exists, not smuggled down here.
"""
from __future__ import annotations

from typing import Any

import httpx

from samvad.llm.base import Completion, Usage, render_prompt

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5-coder"

#: Model ids are prefixed with this in `Usage.model`. Measurement 5 is about
#: whether errors correlate across model families, so a log line that cannot
#: tell a local model from Claude is worthless.
MODEL_PREFIX = "ollama:"

#: (input, output) USD per million tokens -- same shape as claude.py's rate
#: table so cost code can treat the backends uniformly. Local inference is
#: free. This is a real asymmetry, not a placeholder to fill in later.
RATES_USD_PER_MTOK = (0.0, 0.0)

#: Connect fast, read slow. A refused connection is the common case and must
#: not stall the node; a 7B model on a laptop CPU genuinely takes minutes, so
#: the read budget is generous -- but bounded, or a wedged model holds one of
#: the eight concurrency slots forever.
DEFAULT_TIMEOUT = httpx.Timeout(connect=2.0, read=180.0, write=10.0, pool=5.0)


class OllamaError(RuntimeError):
    """Base for every failure of this backend. Catch this one."""


class OllamaUnavailable(OllamaError):
    """The server could not be reached, or could not serve the request.

    Not running, wrong host, model never pulled, timed out. Recoverable in the
    sense that a peer can be told about it and route elsewhere.
    """


class OllamaProtocolError(OllamaError):
    """The server answered, but not with what the API documents.

    A local server's JSON is still untrusted input. Better a named error here
    than an AttributeError three frames into the agent loop.
    """


def _is_chat_prompt(prompt: Any) -> bool:
    """True for a `[{"role": ..., "content": ...}, ...]` message list.

    P2 has not settled the prompt type yet and this module must not be the
    thing that forces the decision. But a chat-shaped prompt flattened into a
    JSON dump reviews badly, and measurement 5 is a reviewing experiment -- so
    the shape that is obviously chat goes to the chat endpoint.
    """
    return (
        isinstance(prompt, list)
        and bool(prompt)
        and all(
            isinstance(turn, dict)
            and isinstance(turn.get("role"), str)
            and isinstance(turn.get("content"), str)
            for turn in prompt
        )
    )


def _count(body: dict[str, Any], field: str) -> int:
    """One token count, validated.

    Absent means zero, not an error: Ollama omits `prompt_eval_count` when it
    served the prefix from its own KV cache. It does not report how many tokens
    that was, so zero is the only honest answer and the completion still stands.
    A count of the wrong type is a different matter -- garbage numbers land in
    the dashboard's running total and nobody notices.
    """
    value = body.get(field)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OllamaProtocolError(f"ollama returned a bad {field}: {value!r}")
    return value


def _usage(body: dict[str, Any], model: str) -> Usage:
    return Usage(
        input=_count(body, "prompt_eval_count"),
        output=_count(body, "eval_count"),
        # Ollama has no prompt-caching API. It reuses a KV cache between calls
        # on the same context, but reports no count for it, so there is nothing
        # honest to put here. Zero, and said out loud rather than invented.
        cache_read=0,
        model=model,
    )


def _snippet(raw: bytes, limit: int = 200) -> str:
    return raw[:limit].decode("utf-8", "replace")


class OllamaBackend:
    """An LLMBackend backed by a local `ollama serve`.

    Args:
        model: Ollama model name, with or without the `ollama:` prefix that
            `config/peers.yaml` uses. Stamped on every Usage, prefixed.
        host: Base URL of the Ollama server.
        timeout: Passed straight to httpx. See DEFAULT_TIMEOUT.
        client: An `httpx.AsyncClient` to use instead of one of our own.
            This is the test seam -- tests inject a client built on
            `httpx.MockTransport`, so nothing in the suite needs a running
            Ollama or touches the network. An injected client is not closed by
            `aclose()`; the caller owns what the caller made.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model.removeprefix(MODEL_PREFIX)
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def model_id(self) -> str:
        """What lands in `Usage.model` and therefore in the log."""
        return f"{MODEL_PREFIX}{self.model}"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the client we created. An injected one is left alone."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def complete(self, prompt: Any) -> Completion:
        if _is_chat_prompt(prompt):
            path, payload, text_of = "/api/chat", {"messages": prompt}, _chat_text
        else:
            path, payload = "/api/generate", {"prompt": render_prompt(prompt)}
            text_of = _generate_text

        body = await self._post(path, {"model": self.model, "stream": False, **payload})
        return Completion(text=text_of(body), usage=_usage(body, self.model_id))

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One request, with the boundary validated on the way back."""
        url = f"{self.host}{path}"
        try:
            response = await self._get_client().post(url, json=payload, timeout=self.timeout)
        except httpx.TimeoutException as exc:
            raise OllamaUnavailable(f"ollama at {self.host} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            # ConnectError included: ollama is not running on this machine.
            raise OllamaUnavailable(f"ollama at {self.host} is unreachable: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            # 404 with {"error": "model ... not found"} is the second most
            # common local failure, after nothing listening at all.
            raise OllamaUnavailable(
                f"ollama at {self.host} returned {response.status_code}: "
                f"{_snippet(response.content)}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise OllamaProtocolError(
                f"ollama returned a body that is not JSON: {_snippet(response.content)}"
            ) from exc

        if not isinstance(body, dict):
            raise OllamaProtocolError(f"ollama returned {type(body).__name__}, expected an object")

        # stream=False means one complete generation. Anything else was cut
        # short, and half a review must never be scored as a whole one.
        if body.get("done") is not True:
            raise OllamaProtocolError(
                f"ollama returned an unfinished generation (done={body.get('done')!r})"
            )
        return body


def _generate_text(body: dict[str, Any]) -> str:
    text = body.get("response")
    if not isinstance(text, str):
        raise OllamaProtocolError(
            f"ollama /api/generate returned no usable 'response' field: {sorted(body)}"
        )
    return text


def _chat_text(body: dict[str, Any]) -> str:
    message = body.get("message")
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str):
        raise OllamaProtocolError(
            f"ollama /api/chat returned no usable 'message.content': {sorted(body)}"
        )
    return text
