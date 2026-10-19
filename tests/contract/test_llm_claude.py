"""Contract: the Anthropic backend. Owner P2. The only backend that costs money.

NO TEST HERE TOUCHES THE NETWORK, AND NONE NEEDS A KEY. Every request goes
through a real `anthropic.AsyncAnthropic` whose HTTP client sits on an
`httpx2.MockTransport` -- the SDK 1.x HTTP layer is httpx2, not httpx -- so the
SDK's own request building and error mapping run for real and nothing leaves
the process. `KEY` below is a placeholder string, not a credential.

Three properties carry the weight. **Usage is the API's own report**, because
every USD figure and the budget that terminates recursion are computed from
it. **The cached prefix is stable**, because a cache that never hits is a cost
multiplier, not a saving. **Nothing reaches this backend by accident**, because
this system spawns recursively and the failure mode is a bill.
"""
from __future__ import annotations

import asyncio
import json

import anthropic
import httpx2
import pytest

from samvad.llm import PaidBackendRefused, backend_for, claude, default_backend
from samvad.llm.base import Completion, Usage, render_prompt
from samvad.llm.claude import (
    ClaudeBackend,
    ClaudeError,
    ClaudeProtocolError,
    ClaudeUnavailable,
)
from samvad.llm.openai_compat import MAX_ATTEMPTS

KEY = "test-key-not-a-real-one"
SYSTEM = "You are agent_a. Reply in JSON. root_task: make the tests pass."


def message_body(text="done", *, uncached=31, output=12, cache_read=0, cache_write=0,
                 stop_reason="end_turn", model="claude-opus-5"):
    """A Messages API response body, shaped as the API documents it."""
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {
            "input_tokens": uncached, "output_tokens": output,
            "cache_creation_input_tokens": cache_write, "cache_read_input_tokens": cache_read,
        },
    }


def sdk_client(handler, **kwargs) -> anthropic.AsyncAnthropic:
    kwargs.setdefault("max_retries", 0)
    transport = httpx2.MockTransport(handler)
    return anthropic.AsyncAnthropic(
        api_key=KEY, http_client=httpx2.AsyncClient(transport=transport), **kwargs
    )


def backend(handler, model: str = "claude-opus-5", **kwargs) -> ClaudeBackend:
    return ClaudeBackend(model=model, client=sdk_client(handler), **kwargs)


def returning(body, *, status: int = 200, headers=None, **kwargs) -> ClaudeBackend:
    def handler(request):
        if isinstance(body, (bytes, str)):
            return httpx2.Response(status, content=body, headers=headers or {})
        return httpx2.Response(status, json=body)

    return backend(handler, **kwargs)


def recording(body=None, **kwargs):
    """A backend plus the decoded request bodies it sent."""
    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=body or message_body())

    return backend(handler, **kwargs), sent


def counting(*responses):
    """A handler that answers with `responses` in turn, repeating the last."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        answer = responses[min(calls["n"], len(responses)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return handler, calls


def rate_limited(retry_after: str | None = None) -> httpx2.Response:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return httpx2.Response(429, headers=headers, json={
        "type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}})


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    """Backoff delays, recorded instead of waited out."""
    delays: list[float] = []

    async def fake_sleep(delay, *args, **kwargs):
        delays.append(delay)

    monkeypatch.setattr(claude.asyncio, "sleep", fake_sleep)
    return delays


# --- the interface ----------------------------------------------------------


async def test_complete_returns_the_completion_from_llm_base():
    completion = await returning(message_body("the plan")).complete("plan it")

    assert isinstance(completion, Completion)
    assert isinstance(completion.usage, Usage)
    assert completion.text == "the plan"


# --- usage: the API's own report, never estimated ----------------------------


async def test_usage_maps_the_reported_counts():
    usage = (await returning(message_body(uncached=140, output=57)).complete("p")).usage
    assert (usage.input, usage.output, usage.cache_read) == (140, 57, 0)
    assert usage.model == "claude-opus-5"


async def test_cache_read_comes_from_cache_read_input_tokens():
    """Measurement 4 exists to measure exactly this number."""
    usage = (await returning(message_body(uncached=50, cache_read=2000)).complete("p")).usage
    assert usage.cache_read == 2000


async def test_input_is_the_whole_prompt_including_cache_hits_and_writes():
    """Anthropic's input_tokens is only the uncached remainder. ContextTracker
    needs the whole prompt -- a cache hit still occupies the window -- and
    usd_for bills input minus cache_read, so the parts are summed."""
    body = message_body(uncached=50, cache_write=1500, cache_read=2000)
    usage = (await returning(body).complete("p")).usage
    assert usage.input == 50 + 1500 + 2000


async def test_usd_for_bills_the_uncached_part_at_the_published_rate():
    from samvad.agent import usd_for

    body = message_body(uncached=1_000_000, cache_read=9_000_000, output=1_000_000)
    usage = (await returning(body).complete("p")).usage
    assert usd_for(usage) == pytest.approx(5.00 + 25.00)


async def test_cache_fields_the_api_leaves_out_are_zero():
    body = message_body()
    del body["usage"]["cache_creation_input_tokens"], body["usage"]["cache_read_input_tokens"]
    usage = (await returning(body).complete("p")).usage
    assert (usage.input, usage.cache_read) == (31, 0)


@pytest.mark.parametrize("usage", [None, {"output_tokens": 3}, {"input_tokens": 3}],
                         ids=["no-usage", "no-input", "no-output"])
async def test_a_paid_completion_without_its_counts_is_refused(usage):
    """A free backend may report nothing. A paid one that did would be priced
    at zero, and a budget that never decrements never stops a spawn tree."""
    body = {**message_body(), "usage": usage}
    with pytest.raises(ClaudeProtocolError):
        await returning(body).complete("p")


async def test_a_non_integer_token_count_raises():
    body = message_body()
    body["usage"]["input_tokens"] = "lots"
    with pytest.raises(ClaudeProtocolError, match="input_tokens"):
        await returning(body).complete("p")


async def test_the_model_id_is_the_configured_one_the_rate_tables_key_on():
    """An API-reported id with a suffix would miss MODEL_RATES and cost $0."""
    body = message_body(model="claude-haiku-4-5-20251001")
    completion = await returning(body, model="claude-haiku-4-5").complete("p")
    assert completion.usage.model == "claude-haiku-4-5"


# --- request shape ----------------------------------------------------------


async def test_a_string_prompt_is_one_user_message_with_no_system():
    b, sent = recording()
    await b.complete("plan the task")

    assert sent[0]["model"] == "claude-opus-5"
    assert sent[0]["messages"] == [{"role": "user", "content": "plan the task"}]
    assert sent[0]["max_tokens"] == claude.DEFAULT_MAX_TOKENS
    assert "system" not in sent[0]


async def test_a_dict_prompt_is_rendered_deterministically():
    """agent._prompt returns a dict. It must reach the model as stable JSON."""
    prompt = {"turn": 3, "root_task": "make the tests pass"}
    b, sent = recording()
    await b.complete(prompt)
    assert sent[0]["messages"][0]["content"] == render_prompt(prompt)


async def test_a_chat_prompt_sends_its_system_turn_as_the_system_param():
    """The Messages API has no system role; a system turn left in `messages`
    is a 400, and folded into a user turn it is no longer a cacheable prefix."""
    b, sent = recording()
    await b.complete([{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": "go"}])

    assert [block["text"] for block in sent[0]["system"]] == [SYSTEM]
    assert sent[0]["messages"] == [{"role": "user", "content": "go"}]


async def test_the_system_prefix_carries_the_only_cache_breakpoint():
    b, sent = recording()
    await b.complete([{"role": "system", "content": "frozen rules"},
                      {"role": "system", "content": SYSTEM},
                      {"role": "user", "content": "first"},
                      {"role": "assistant", "content": "ok"},
                      {"role": "user", "content": "second"}])

    assert sent[0]["system"] == [
        {"type": "text", "text": "frozen rules"},
        {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}},
    ]
    assert "cache_control" not in json.dumps(sent[0]["messages"])


async def test_the_cached_prefix_is_byte_identical_from_turn_to_turn():
    """Caching is a prefix match. Anything this layer added to the prefix -- a
    timestamp, an id, a reordering -- would make every call a cache miss."""
    b, sent = recording()
    for latest in ("turn one", "turn two, with a different id: m-0042"):
        await b.complete([{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": latest}])

    first, second = (json.dumps(s["system"], sort_keys=True) for s in sent)
    assert first == second
    assert sent[0]["messages"] != sent[1]["messages"]


async def test_a_string_prompt_gets_no_breakpoint():
    """All of it is volatile. A cache write costs more than plain input and a
    prefix that never repeats is never read."""
    b, sent = recording()
    await b.complete("everything changes every turn")
    assert "cache_control" not in json.dumps(sent[0])


@pytest.mark.parametrize("prompt", [
    [{"role": "system", "content": SYSTEM}],
    [{"role": "tool", "content": "42"}],
], ids=["only-system", "unknown-role"])
async def test_a_chat_prompt_the_api_cannot_take_fails_before_any_request(prompt):
    b, sent = recording()
    with pytest.raises(ValueError):
        await b.complete(prompt)
    assert sent == []


# --- what comes back --------------------------------------------------------


async def test_only_text_blocks_become_the_completion_text():
    """Thinking is on by default on Opus 5, so content[0] is often not text."""
    body = message_body()
    body["content"] = [{"type": "thinking", "thinking": "", "signature": "sig"},
                       {"type": "text", "text": "part one, "},
                       {"type": "text", "text": "part two"}]
    assert (await returning(body).complete("p")).text == "part one, part two"


@pytest.mark.parametrize("change", [
    {"content": None},
    {"content": []},
    {"content": [{"type": "thinking", "thinking": "", "signature": "sig"}]},
    {"content": [{"type": "text"}]},
    {"content": "not a list"},
], ids=["no-content", "empty", "no-text-block", "text-block-without-text", "not-a-list"])
async def test_a_reshaped_response_fails_here_not_in_the_agent_loop(change):
    with pytest.raises(ClaudeProtocolError):
        await returning({**message_body(), **change}).complete("p")


async def test_an_empty_object_is_rejected():
    with pytest.raises(ClaudeProtocolError):
        await returning({}).complete("p")


async def test_non_json_is_rejected_and_the_error_says_what_arrived():
    with pytest.raises(ClaudeProtocolError, match="502 Bad Gateway"):
        await returning(b"<html>502 Bad Gateway</html>").complete("p")


async def test_a_json_array_is_rejected():
    body = b"[1, 2, 3]"
    with pytest.raises(ClaudeProtocolError):
        await returning(body, headers={"content-type": "application/json"}).complete("p")


async def test_a_truncated_json_body_is_rejected():
    body = b'{"content": [{"type": "te'
    with pytest.raises(ClaudeProtocolError):
        await returning(body, headers={"content-type": "application/json"}).complete("p")


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
async def test_an_incomplete_answer_raises_but_keeps_what_it_cost(stop_reason):
    """Half an answer must never be scored as a whole one -- but it was billed,
    and a budget that only counts the successes is not conserved."""
    body = message_body("half an ans", uncached=40, output=99, stop_reason=stop_reason)
    with pytest.raises(ClaudeProtocolError, match=stop_reason) as caught:
        await returning(body).complete("p")
    assert caught.value.usage == Usage(input=40, output=99, cache_read=0, model="claude-opus-5")


# --- retries: one policy, shared with openai_compat --------------------------


async def test_429_is_retried_with_exponential_backoff_and_then_succeeds(slept):
    handler, calls = counting(rate_limited(), rate_limited(),
                              httpx2.Response(200, json=message_body("finally")))

    assert (await backend(handler).complete("p")).text == "finally"
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]


async def test_retry_after_is_honoured_when_the_api_sends_it(slept):
    handler, calls = counting(rate_limited("7"), httpx2.Response(200, json=message_body()))

    await backend(handler).complete("p")
    assert calls["n"] == 2
    assert slept == [7.0]


async def test_a_rate_limit_that_never_lifts_gives_up_legibly(slept):
    handler, calls = counting(rate_limited("0"))

    with pytest.raises(ClaudeUnavailable, match="429"):
        await backend(handler).complete("p")
    assert calls["n"] == MAX_ATTEMPTS
    assert len(slept) == MAX_ATTEMPTS - 1


async def test_the_sdk_does_not_retry_behind_our_back(slept):
    """Left on, the SDK's own retries would multiply ours: 4 x 3 requests at
    two different backoff rates, against a key that is already rate limited."""
    handler, calls = counting(rate_limited("0"))
    b = ClaudeBackend(client=sdk_client(handler, max_retries=2))

    with pytest.raises(ClaudeUnavailable):
        await b.complete("p")
    assert calls["n"] == MAX_ATTEMPTS
    assert ClaudeBackend(api_key=KEY)._get_client().max_retries == 0


async def test_an_overloaded_api_is_retried(slept):
    overloaded = httpx2.Response(529, json={
        "type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    handler, calls = counting(overloaded, httpx2.Response(200, json=message_body()))

    await backend(handler).complete("p")
    assert calls["n"] == 2


async def test_a_401_is_not_retried_and_never_echoes_the_key(slept):
    handler, calls = counting(httpx2.Response(401, json={
        "type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))

    with pytest.raises(ClaudeUnavailable) as caught:
        await backend(handler).complete("p")
    assert calls["n"] == 1
    assert KEY not in str(caught.value)
    assert "ANTHROPIC_API_KEY" in str(caught.value)
    assert "rotate" in str(caught.value).lower()


async def test_a_bad_request_is_not_retried(slept):
    handler, calls = counting(httpx2.Response(400, json={
        "type": "error", "error": {"type": "invalid_request_error", "message": "bad model"}}))

    with pytest.raises(ClaudeUnavailable, match="400"):
        await backend(handler).complete("p")
    assert calls["n"] == 1


async def test_connection_refused_raises_unavailable_after_retrying(slept):
    handler, calls = counting(httpx2.ConnectError("Connection refused"))

    with pytest.raises(ClaudeUnavailable, match="unreachable"):
        await backend(handler).complete("p")
    assert calls["n"] == MAX_ATTEMPTS


async def test_a_timeout_raises_unavailable_and_says_so(slept):
    handler, _ = counting(httpx2.ReadTimeout("timed out"))

    with pytest.raises(ClaudeUnavailable, match="timed out"):
        await backend(handler).complete("p")


def test_every_failure_shares_one_catchable_base():
    """agent.py catches ClaudeError once and turns it into a protocol state."""
    assert issubclass(ClaudeUnavailable, ClaudeError)
    assert issubclass(ClaudeProtocolError, ClaudeError)


# --- the key ----------------------------------------------------------------


def test_a_missing_key_names_the_env_var_not_a_value(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ClaudeUnavailable, match="ANTHROPIC_API_KEY") as caught:
        ClaudeBackend()
    assert ".env" in str(caught.value)


def test_an_empty_key_is_as_missing_as_an_absent_one(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(ClaudeUnavailable):
        ClaudeBackend()


def test_the_key_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert ClaudeBackend()._get_client().api_key == KEY


# --- rates and windows ------------------------------------------------------


def test_a_model_without_a_published_rate_is_refused():
    """usd_for prices an unknown model at zero. A paid model at zero never
    decrements a budget, and the budget is what terminates recursion."""
    with pytest.raises(ClaudeError, match="RATES_USD_PER_MTOK"):
        ClaudeBackend(model="claude-opus-9", api_key=KEY)


def test_the_rate_table_matches_the_one_agent_bills_from():
    from samvad.agent import MODEL_RATES

    billed = {m: r for m, r in MODEL_RATES.items() if m.startswith("claude-")}
    assert claude.RATES_USD_PER_MTOK == billed


def test_every_rated_model_has_a_context_window():
    """Otherwise ContextTracker falls back to 8K and backpressure fires early."""
    from samvad.store.context import MODEL_LIMITS

    assert set(claude.RATES_USD_PER_MTOK) <= set(MODEL_LIMITS)


# --- routing: paid only on purpose -------------------------------------------


def test_the_factory_refuses_claude_without_the_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("SAMVAD_ALLOW_PAID", raising=False)
    with pytest.raises(PaidBackendRefused, match="SAMVAD_ALLOW_PAID") as caught:
        backend_for("claude-opus-5", api_key=KEY)
    assert "$5.00" in str(caught.value)


def test_only_exactly_one_opts_in(monkeypatch):
    monkeypatch.setenv("SAMVAD_ALLOW_PAID", "0")
    with pytest.raises(PaidBackendRefused):
        backend_for("claude-opus-5", api_key=KEY)


def test_the_factory_builds_claude_when_paid_is_allowed(monkeypatch):
    monkeypatch.setenv("SAMVAD_ALLOW_PAID", "1")
    made = backend_for("claude-haiku-4-5", api_key=KEY)
    assert isinstance(made, ClaudeBackend)
    assert made.model_id == "claude-haiku-4-5"


def test_mock_llm_still_wins_over_an_allowed_paid_model(monkeypatch):
    from samvad.llm.mock import MockBackend

    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("SAMVAD_ALLOW_PAID", "1")
    assert isinstance(default_backend("claude-opus-5"), MockBackend)


def test_a_paid_name_with_no_backend_is_still_refused(monkeypatch):
    monkeypatch.setenv("SAMVAD_ALLOW_PAID", "1")
    with pytest.raises(PaidBackendRefused):
        backend_for("gpt-5", api_key=KEY)


# --- concurrency ------------------------------------------------------------


async def test_concurrent_calls_do_not_deadlock():
    """The supervisor drives eight of these at once (concurrency cap of 8)."""
    b = returning(message_body(output=7))
    results = await asyncio.gather(*(b.complete(f"p{i}") for i in range(8)))
    assert [r.usage.output for r in results] == [7] * 8
