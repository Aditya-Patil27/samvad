"""Contract: the local Ollama backend. The `hetero` half of measurement 5.

Without a working local backend there is no hetero condition, and measurement 5
-- "the result this whole design is built around" -- cannot run at all.

Every request here goes through an injected `httpx.MockTransport`. **No test in
this file touches the network or needs a running Ollama.** A test that only
passes on the one laptop with `ollama serve` up is a failed test; three of the
four machines will not have it.

Two properties carry the weight. **Usage on every completion** -- the dashboard
sums cost across the mesh from `input`/`output`/`cache_read` plus a model id.
And **legible failure** -- Ollama being down is the common case, so it must
raise a typed error the agent loop can turn into a protocol state, never a
confusing AttributeError from deep inside the loop and never a hang.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from samvad.llm.base import Completion, Usage
from samvad.llm.ollama import (
    OllamaBackend,
    OllamaError,
    OllamaProtocolError,
    OllamaUnavailable,
)

# A real /api/generate body, trimmed of the duration fields we do not use.
REAL_GENERATE_RESPONSE = {
    "model": "qwen2.5-coder",
    "created_at": "2026-01-14T09:41:02.117Z",
    "response": "The bug is the off-by-one in the loop bound.",
    "done": True,
    "done_reason": "stop",
    "total_duration": 4_883_583_458,
    "load_duration": 1_334_875,
    "prompt_eval_count": 26,
    "prompt_eval_duration": 342_546_000,
    "eval_count": 298,
    "eval_duration": 4_535_599_000,
}


def backend_returning(payload, *, status: int = 200, **kwargs) -> OllamaBackend:
    """A backend wired to a transport that answers with `payload`, once."""
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    return OllamaBackend(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)


def backend_raising(exc: Exception, **kwargs) -> OllamaBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return OllamaBackend(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)


def recording_backend(payload=REAL_GENERATE_RESPONSE, **kwargs):
    """A backend plus the list of requests it sent."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaBackend(client=client, **kwargs), sent


# --- the interface ----------------------------------------------------------


async def test_complete_returns_the_completion_from_llm_base():
    """The same Completion every backend returns -- not a look-alike."""
    completion = await backend_returning(REAL_GENERATE_RESPONSE).complete("review this")

    assert isinstance(completion, Completion)
    assert isinstance(completion.usage, Usage)
    assert completion.text == "The bug is the off-by-one in the loop bound."


# --- usage mapping ----------------------------------------------------------


async def test_usage_maps_ollamas_counts_onto_the_shared_fields():
    """prompt_eval_count -> input, eval_count -> output. Honestly, both ways."""
    usage = (await backend_returning(REAL_GENERATE_RESPONSE).complete("p")).usage

    assert usage.input == 26
    assert usage.output == 298


async def test_cache_read_is_zero_because_ollama_reports_no_prompt_cache():
    """Ollama has no prompt-caching API surface and reports no reuse count.
    Zero is the honest value; inventing one would corrupt measurement 4."""
    assert (await backend_returning(REAL_GENERATE_RESPONSE).complete("p")).usage.cache_read == 0


async def test_the_model_id_names_the_family_not_just_the_model():
    """Measurement 5 is about uncorrelated errors. A log line that cannot tell
    a local model from Claude proves nothing, so the id is prefixed."""
    backend = backend_returning(REAL_GENERATE_RESPONSE, model="qwen2.5-coder")
    completion = await backend.complete("p")
    assert completion.usage.model == "ollama:qwen2.5-coder"


async def test_an_already_prefixed_model_id_is_not_prefixed_twice():
    """config/peers.example.yaml writes it as `ollama:qwen2.5-coder`."""
    backend = backend_returning(REAL_GENERATE_RESPONSE, model="ollama:qwen2.5-coder")
    assert (await backend.complete("p")).usage.model == "ollama:qwen2.5-coder"


async def test_the_configured_model_is_what_gets_requested():
    backend, sent = recording_backend(model="ollama:qwen2.5-coder")
    await backend.complete("p")

    assert json.loads(sent[0].content)["model"] == "qwen2.5-coder"


async def test_streaming_is_off_so_one_request_is_one_completion():
    backend, sent = recording_backend()
    await backend.complete("plan the task")

    body = json.loads(sent[0].content)
    assert body["stream"] is False
    assert body["prompt"] == "plan the task"
    assert sent[0].url.path == "/api/generate"


async def test_a_missing_prompt_eval_count_is_recorded_as_zero_not_invented():
    """Ollama omits prompt_eval_count when it served the prefix from its own KV
    cache. It does not say how many tokens that was, so the count is zero and
    the completion still arrives -- a made-up number would be worse."""
    payload = {k: v for k, v in REAL_GENERATE_RESPONSE.items() if k != "prompt_eval_count"}
    usage = (await backend_returning(payload).complete("p")).usage

    assert usage.input == 0
    assert usage.output == 298


# --- chat-shaped prompts ----------------------------------------------------


async def test_a_chat_shaped_prompt_goes_to_the_chat_endpoint():
    """P2 has not settled the prompt type. A role/content list must not reach
    the model as a JSON dump -- a reviewer fed a dump reviews badly, and
    measurement 5 is a reviewing experiment."""
    chat_response = {
        "model": "qwen2.5-coder",
        "message": {"role": "assistant", "content": "looks correct"},
        "done": True,
        "prompt_eval_count": 12,
        "eval_count": 3,
    }
    backend, sent = recording_backend(chat_response)

    completion = await backend.complete([{"role": "user", "content": "review this"}])

    assert sent[0].url.path == "/api/chat"
    assert json.loads(sent[0].content)["messages"] == [{"role": "user", "content": "review this"}]
    assert completion.text == "looks correct"
    assert completion.usage.input == 12
    assert completion.usage.output == 3


# --- malformed and truncated responses --------------------------------------


async def test_a_body_that_is_not_json_raises_a_typed_error():
    """A local server's JSON is still untrusted input (CLAUDE.md)."""
    with pytest.raises(OllamaProtocolError):
        await backend_returning(b"<html>502 Bad Gateway</html>").complete("p")


async def test_a_truncated_body_raises_rather_than_returning_half_an_answer():
    with pytest.raises(OllamaProtocolError):
        await backend_returning(b'{"response": "half an ans').complete("p")


async def test_a_response_missing_its_text_field_raises_not_attributeerror():
    """This is the failure that must never surface deep in the agent loop."""
    payload = {k: v for k, v in REAL_GENERATE_RESPONSE.items() if k != "response"}
    with pytest.raises(OllamaProtocolError):
        await backend_returning(payload).complete("p")


async def test_an_unfinished_generation_raises():
    """done=false means the generation was cut short. Accepting it would let a
    half-written review be scored as a real one."""
    with pytest.raises(OllamaProtocolError):
        await backend_returning({**REAL_GENERATE_RESPONSE, "done": False}).complete("p")


async def test_a_non_integer_token_count_raises():
    """Garbage counts are worse than no counts -- they land in the running
    cost total on the dashboard and nobody notices."""
    with pytest.raises(OllamaProtocolError):
        await backend_returning({**REAL_GENERATE_RESPONSE, "eval_count": "lots"}).complete("p")


async def test_a_json_body_that_is_not_an_object_raises():
    with pytest.raises(OllamaProtocolError):
        await backend_returning(b"[1, 2, 3]").complete("p")


async def test_the_protocol_error_says_what_arrived():
    """Legible: a human reading the log should not have to reproduce it."""
    with pytest.raises(OllamaProtocolError) as caught:
        await backend_returning(b"<html>502 Bad Gateway</html>").complete("p")
    assert "502 Bad Gateway" in str(caught.value)


# --- ollama not running -----------------------------------------------------


async def test_connection_refused_raises_unavailable():
    """The common case: three of the four laptops have no ollama running."""
    backend = backend_raising(httpx.ConnectError("[Errno 111] Connection refused"))

    with pytest.raises(OllamaUnavailable):
        await backend.complete("p")


async def test_connection_refused_names_the_host_it_tried():
    backend = backend_raising(
        httpx.ConnectError("Connection refused"), host="http://192.168.1.43:11434"
    )
    with pytest.raises(OllamaUnavailable) as caught:
        await backend.complete("p")

    assert "192.168.1.43:11434" in str(caught.value)


async def test_connection_refused_fails_fast_rather_than_hanging():
    """A node that hangs on a dead peer's backend blocks its whole inbox."""
    backend = backend_raising(httpx.ConnectError("Connection refused"))

    async with asyncio.timeout(5):
        with pytest.raises(OllamaUnavailable):
            await backend.complete("p")


async def test_a_read_timeout_raises_unavailable():
    """Local inference is slow, but not unbounded -- a wedged model must not
    hold the node's concurrency slot forever."""
    backend = backend_raising(httpx.ReadTimeout("timed out"))

    with pytest.raises(OllamaUnavailable):
        await backend.complete("p")


async def test_a_connect_timeout_raises_unavailable():
    backend = backend_raising(httpx.ConnectTimeout("timed out"))

    with pytest.raises(OllamaUnavailable):
        await backend.complete("p")


async def test_the_timeout_error_says_it_timed_out():
    backend = backend_raising(httpx.ReadTimeout("timed out"))
    with pytest.raises(OllamaUnavailable) as caught:
        await backend.complete("p")

    assert "timed out" in str(caught.value).lower()


async def test_a_model_that_was_never_pulled_raises_unavailable():
    """404 {"error": "model not found"} -- the second most common local case."""
    backend = backend_returning({"error": 'model "qwen2.5-coder" not found'}, status=404)

    with pytest.raises(OllamaUnavailable) as caught:
        await backend.complete("p")
    assert "not found" in str(caught.value)


async def test_a_server_error_raises_unavailable():
    backend = backend_returning({"error": "internal"}, status=500)

    with pytest.raises(OllamaUnavailable):
        await backend.complete("p")


async def test_both_failure_modes_share_one_catchable_base():
    """agent.py catches OllamaError once and turns it into a protocol state."""
    assert issubclass(OllamaUnavailable, OllamaError)
    assert issubclass(OllamaProtocolError, OllamaError)


# --- concurrency ------------------------------------------------------------


async def test_concurrent_calls_do_not_deadlock():
    """The supervisor drives eight of these at once (concurrency cap of 8)."""
    backend = backend_returning(REAL_GENERATE_RESPONSE)

    results = await asyncio.gather(*(backend.complete(f"p{i}") for i in range(8)))

    assert len(results) == 8
    assert all(r.usage.output == 298 for r in results)


# --- cost -------------------------------------------------------------------


def test_local_inference_is_free_and_says_so():
    """A real asymmetry against the Claude backend, not a rounding error.
    Measurement 5 compares homo and hetero at matched *token* cost precisely
    because the USD cost of the hetero half is zero."""
    from samvad.llm import ollama

    assert ollama.RATES_USD_PER_MTOK == (0.0, 0.0)
