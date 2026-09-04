"""Contract: the mock backend. Seam between P2's agent core and every other layer.

Deliberately NOT gated on protocol.py. MOCK_LLM=1 is what P1, P3 and P4 run the
wire against hundreds of times, so it has to work before the envelope exists.

Two properties carry the weight. **Usage on every completion** -- the dashboard
sums cost across the mesh from `input`/`output`/`cache_read` plus a model id, so
a completion without them makes measurement 4 unmeasurable. And **it never hangs
or exhausts** -- a fake that raises StopIteration on the reply someone forgot to
queue turns a transport bug hunt into a fake-LLM bug hunt.

No test here may call a live API. Nothing in this module has a network path.
"""
from __future__ import annotations

import asyncio

from samvad.llm.base import Completion, Usage
from samvad.llm.mock import MockBackend

# --- the interface ----------------------------------------------------------


async def test_complete_returns_the_completion_from_llm_base():
    """The same Completion every backend returns -- not a look-alike."""
    completion = await MockBackend().complete("plan the task")
    assert isinstance(completion, Completion)
    assert isinstance(completion.usage, Usage)
    assert isinstance(completion.text, str)
    assert completion.text


async def test_every_completion_carries_usage_and_a_model_id():
    """The dashboard sums cost across the mesh from exactly these fields."""
    usage = (await MockBackend().complete("plan the task")).usage
    assert usage.input > 0
    assert usage.output > 0
    assert usage.cache_read >= 0
    assert usage.model


async def test_the_model_id_is_configurable():
    """Measurement 5 needs two model families distinguishable in the log."""
    completion = await MockBackend(model="ollama:qwen2.5-coder").complete("p")
    assert completion.usage.model == "ollama:qwen2.5-coder"


# --- determinism ------------------------------------------------------------


async def test_the_same_prompt_gives_the_same_answer():
    """Zero cost is only half of it; a mock that varies makes a flaky suite."""
    a = await MockBackend().complete("identical prompt")
    b = await MockBackend().complete("identical prompt")
    assert a.text == b.text
    assert a.usage == b.usage


async def test_token_counts_track_the_prompt_rather_than_being_a_constant():
    """A flat usage number would let a context-accounting bug pass unnoticed."""
    small = await MockBackend().complete("hi")
    large = await MockBackend().complete("hi" * 4000)
    assert large.usage.input > small.usage.input


# --- queued replies ---------------------------------------------------------


async def test_queued_replies_come_back_in_order():
    backend = MockBackend(replies=["first", "second", "third"])
    assert (await backend.complete("p")).text == "first"
    assert (await backend.complete("p")).text == "second"
    assert (await backend.complete("p")).text == "third"


async def test_the_queue_never_exhausts():
    """A test must never hang or crash on a reply someone forgot to queue."""
    backend = MockBackend(replies=["only one"])
    for _ in range(5):
        assert (await backend.complete("p")).text == "only one"
    assert backend.call_count == 5


async def test_replies_can_be_queued_after_construction():
    backend = MockBackend()
    backend.queue('{"task_status": "complete", "result": "done!"}')
    assert "task_status" in (await backend.complete("p")).text


async def test_prompts_are_recorded_for_inspection():
    """Asserting on what the agent actually sent is how prompt-cache prefix
    regressions get caught without spending money."""
    backend = MockBackend()
    await backend.complete("first prompt")
    await backend.complete("second prompt")
    assert backend.prompts == ["first prompt", "second prompt"]
    assert backend.call_count == 2


# --- never hangs ------------------------------------------------------------


async def test_a_completion_arrives_promptly():
    """No sleeps, no retries, no network -- CI runs this on every push."""
    async with asyncio.timeout(5):
        await MockBackend().complete("p")


async def test_concurrent_calls_do_not_deadlock():
    """The supervisor drives eight of these at once."""
    backend = MockBackend(replies=["a", "b"])
    results = await asyncio.gather(*(backend.complete(f"p{i}") for i in range(8)))
    assert len(results) == 8
    assert backend.call_count == 8


# --- non-string prompts -----------------------------------------------------


async def test_a_structured_prompt_is_accepted_and_counted():
    """Prompts are not settled yet (P2 owns them). The mock must not care."""
    prompt = [{"role": "user", "content": "implement binary search"}]
    completion = await MockBackend().complete(prompt)
    assert completion.usage.input > 0


# --- cache accounting -------------------------------------------------------


async def test_cache_read_is_reported_on_a_repeated_prefix():
    """cache_read persistently zero is the symptom the real backend hunts for;
    the mock has to be able to produce a non-zero one at all."""
    backend = MockBackend()
    first = await backend.complete("a stable system prompt")
    second = await backend.complete("a stable system prompt")
    assert first.usage.cache_read == 0
    assert second.usage.cache_read > 0


async def test_cache_simulation_can_be_switched_off():
    backend = MockBackend(simulate_cache=False)
    await backend.complete("a stable system prompt")
    second = await backend.complete("a stable system prompt")
    assert second.usage.cache_read == 0
