"""Contract: hosted OpenAI-compatible backends (Groq, NVIDIA NIM). Owner P2.

NO TEST HERE TOUCHES THE NETWORK. Every request goes through an injected
httpx.MockTransport, so the suite needs no key, no quota, and no internet --
which is also why it can run in CI where none of those exist.

Not gated on protocol.py: a backend never sees an envelope.
"""
from __future__ import annotations

import httpx
import pytest

from samvad.llm import backend_for
from samvad.llm.openai_compat import (
    OpenAICompatBackend,
    ProviderProtocolError,
    ProviderUnavailable,
    split_model,
)

KEY = "test-key-not-a-real-one"


def ok_body(text: str = "done", prompt_tokens: int = 31, completion_tokens: int = 12):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def backend(handler, model: str = "groq:llama-3.3-70b-versatile") -> OpenAICompatBackend:
    return OpenAICompatBackend(
        model=model, api_key=KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


# --- model strings ----------------------------------------------------------


def test_provider_and_model_split_on_the_first_colon_only():
    """NVIDIA ids contain a slash and Ollama-style ids contain colons -- a
    naive split would mangle exactly the ids most likely to be used."""
    provider, model = split_model("nvidia:meta/llama-3.1-70b-instruct")
    assert provider.name == "nvidia"
    assert model == "meta/llama-3.1-70b-instruct"


def test_unknown_provider_is_rejected():
    with pytest.raises(Exception, match="unknown provider"):
        split_model("openai:gpt-4")


def test_provider_without_a_model_is_rejected():
    with pytest.raises(Exception, match="no model"):
        split_model("groq:")


def test_factory_routes_hosted_prefixes():
    made = backend_for("groq:llama-3.3-70b-versatile", api_key=KEY)
    assert isinstance(made, OpenAICompatBackend)
    assert made.model_id == "groq:llama-3.3-70b-versatile"


# --- the key never leaks ----------------------------------------------------


def test_a_missing_key_names_the_env_var_not_a_value(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailable) as exc:
        OpenAICompatBackend(model="groq:llama-3.3-70b-versatile")
    assert "GROQ_API_KEY" in str(exc.value)


def test_a_rejected_key_is_never_echoed_in_the_error():
    """A 401 message that quotes the key puts it in every log that caught it."""

    def handler(request):
        return httpx.Response(401, json={"error": "invalid api key"})

    with pytest.raises(ProviderUnavailable) as exc:
        import asyncio

        asyncio.run(backend(handler).complete("hi"))
    assert KEY not in str(exc.value)
    assert "rotate" in str(exc.value).lower()


async def test_the_key_is_sent_as_a_bearer_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=ok_body())

    await backend(handler).complete("hi")
    assert seen["auth"] == f"Bearer {KEY}"


# --- usage and cost ---------------------------------------------------------


async def test_usage_maps_from_the_openai_shape():
    def handler(request):
        return httpx.Response(200, json=ok_body(prompt_tokens=140, completion_tokens=57))

    completion = await backend(handler).complete("hi")
    assert completion.text == "done"
    assert completion.usage.input == 140
    assert completion.usage.output == 57
    assert completion.usage.model == "groq:llama-3.3-70b-versatile"


async def test_cache_read_defaults_to_zero_rather_than_being_inferred():
    """Most free tiers never report cached tokens. A guessed number would
    corrupt measurement 4, which exists to measure exactly that."""

    def handler(request):
        return httpx.Response(200, json=ok_body())

    assert (await backend(handler).complete("hi")).usage.cache_read == 0


async def test_a_hosted_free_tier_call_costs_nothing():
    from samvad.agent import usd_for

    def handler(request):
        return httpx.Response(200, json=ok_body(prompt_tokens=9999, completion_tokens=9999))

    assert usd_for((await backend(handler).complete("hi")).usage) == 0.0


# --- rate limits ------------------------------------------------------------


async def test_429_is_retried_and_then_succeeds():
    """P2 definition-of-done: 429 responses retry with exponential backoff.

    On a free tier at experiment volume 429 is the steady state, not an error.
    """
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})
        return httpx.Response(200, json=ok_body())

    assert (await backend(handler).complete("hi")).text == "done"
    assert calls["n"] == 2


async def test_a_401_is_not_retried():
    """A bad key does not become valid by being sent again -- retrying only
    turns a clear failure into a slow one."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": "nope"})

    with pytest.raises(ProviderUnavailable):
        await backend(handler).complete("hi")
    assert calls["n"] == 1


# --- boundary validation ----------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [{}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}],
    ids=["empty", "no-choices", "no-message", "no-content"],
)
async def test_a_reshaped_response_fails_here_not_in_the_agent_loop(payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    with pytest.raises(ProviderProtocolError):
        await backend(handler).complete("hi")


async def test_non_json_is_rejected():
    def handler(request):
        return httpx.Response(200, text="<html>gateway</html>")

    with pytest.raises(ProviderProtocolError):
        await backend(handler).complete("hi")
