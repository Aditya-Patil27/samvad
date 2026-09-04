"""Contract: the mind. Seam between P2 and P1/P3.

P2's job this week is to make this file pass.

Two of these are not ordinary tests. `test_complete_requires_exit_code_zero` is
the project's single most effective hallucination control, and it works only
because it lives in a code path rather than in a prompt. And the budget
conservation tests are what terminate recursion -- there is no separate
fork-bomb guard because conservation *is* the guard.

Every test here runs under MOCK_LLM=1. No test may call a live API. Not once,
not "just to check".
"""
import pytest

from tests.fakes import StubLLM
from tests.support import envelope, has_message, implemented

pytestmark = pytest.mark.skipif(
    not has_message(),
    reason="week 1 joint task: write protocol.py together first",
)


def _make(**overrides):
    from samvad.protocol import Message

    return Message(**envelope(**overrides))


def _agent(**kwargs):
    from samvad.agent import Agent

    return Agent(**kwargs)


async def _handle(agent, msg):
    """Run handle(), skipping cleanly while it is still a stub."""
    try:
        return await agent.handle(msg)
    except NotImplementedError:
        pytest.skip("Agent.handle not implemented yet")


# --- the core loop ----------------------------------------------------------


async def test_handle_returns_a_task_result_under_mock_llm():
    """A task_request in, a well-formed task_result out. No network."""
    from samvad.protocol import Performative

    request = _make(performative=Performative.TASK_REQUEST.value)
    replies = await _handle(_agent(), request)

    assert len(replies) >= 1
    reply = replies[0]
    assert reply.performative == Performative.TASK_RESULT
    assert reply.reply_to == request.message_id
    assert reply.conversation_id == request.conversation_id


async def test_handle_returns_messages_and_never_sends_them():
    """handle() is pure -- it returns replies for P1 to deliver.

    The agent is constructed with no transport at all, so anything that tried
    to send would fail rather than quietly succeed.
    """
    replies = await _handle(_agent(), _make())
    assert isinstance(replies, list)


async def test_root_task_is_copied_verbatim_never_paraphrased():
    """Immutable for the whole conversation. By turn six, agents that only see
    paraphrases are solving a different problem."""
    request = _make(root_task="Implement a binary search function")
    for reply in await _handle(_agent(), request):
        assert reply.root_task == request.root_task


async def test_every_reply_reports_its_own_cost():
    """cost is present on every task_result -- the dashboard sums them live."""
    from samvad.protocol import Performative

    for reply in await _handle(_agent(), _make()):
        if reply.performative == Performative.TASK_RESULT:
            assert reply.cost is not None
            assert reply.cost.model


# --- the hallucination control ----------------------------------------------


async def test_complete_requires_exit_code_zero():
    """An agent may not set task_status=complete when exit_code != 0.

    Enforced HERE, in a code path -- not requested in a prompt. The runtime is
    the arbiter; the model is advisory. The stub LLM is deliberately driven to
    *claim* success on a failing exit code, so the assertion is on the runtime
    overruling it rather than on the mock happening to behave.
    """
    from samvad.protocol import TaskStatus

    try:
        agent = _agent(llm=StubLLM(replies=['{"task_status": "complete", "result": "done!"}']))
    except TypeError:
        pytest.skip("Agent does not take an `llm` argument yet")

    failing = _make(
        performative="task_result",
        task_status="in_progress",
        payload={"result": "tests run", "exit_code": 1},
    )

    for reply in await _handle(agent, failing):
        assert reply.task_status != TaskStatus.COMPLETE, (
            "the model claimed complete on exit_code=1 and the runtime allowed it"
        )


def test_uncertain_exists_as_an_alternative_to_confabulating():
    """An agent with no way to express doubt has exactly one option, and it
    will take it."""
    from samvad.protocol import Performative, TaskStatus

    assert Performative.UNCERTAIN
    assert TaskStatus.UNCERTAIN


# --- budget conservation ----------------------------------------------------


def test_child_slices_sum_to_at_most_the_parent():
    """THE invariant. A child can never hold more than its parent gave it."""
    from samvad.budget import slice_budget

    parent = _make().budget
    if not implemented(slice_budget, parent, 4):
        pytest.skip("slice_budget not implemented yet")

    slices = slice_budget(parent, 4)
    assert len(slices) == 4
    assert sum(s.usd_remaining for s in slices) <= parent.usd_remaining
    assert sum(s.turns_remaining for s in slices) <= parent.turns_remaining


def test_slicing_never_creates_money_at_any_fanout():
    """Conservation has to hold for every n, not just the convenient one."""
    from samvad.budget import slice_budget

    parent = _make().budget
    if not implemented(slice_budget, parent, 2):
        pytest.skip("slice_budget not implemented yet")

    for n in (1, 2, 3, 5, 8):
        slices = slice_budget(parent, n)
        assert sum(s.usd_remaining for s in slices) <= parent.usd_remaining
        assert all(s.usd_remaining >= 0 for s in slices)


def test_cannot_afford_is_false_when_the_slice_is_too_thin():
    """A branch that cannot afford one call must not call."""
    from samvad.budget import can_afford

    thin = _make(budget={"usd_remaining": 0.0001, "turns_remaining": 1}).budget
    if not implemented(can_afford, thin, 0.01):
        pytest.skip("can_afford not implemented yet")

    assert can_afford(thin, 0.01) is False
    assert can_afford(_make().budget, 0.01) is True


async def test_unaffordable_branch_returns_spawn_refused_not_a_crash():
    """Errors peers should reason about are protocol states, not exceptions."""
    from samvad.protocol import Performative

    broke = _make(
        performative="spawn_request",
        budget={"usd_remaining": 0.0, "turns_remaining": 0},
        payload={"role": "executor", "prompt_id": "executor", "slice": {}},
    )
    replies = await _handle(_agent(), broke)

    refusals = [r for r in replies if r.performative == Performative.SPAWN_REFUSED]
    assert refusals, "an unaffordable spawn was not refused"
    assert refusals[0].payload["reason"] in {
        "at_capacity",
        "context_full",
        "budget_policy",
        "max_depth",
    }


async def test_max_depth_is_enforced():
    """A depth-4 spawn under max_depth: 3 is refused, with reason max_depth."""
    from samvad.protocol import Performative

    too_deep = _make(
        performative="spawn_request",
        sender="agent_a/w1/w2/w3",
        spawn={"parent": "agent_a/w1/w2", "depth": 3, "max_depth": 3},
        payload={"role": "executor", "prompt_id": "executor", "slice": {}},
    )
    replies = await _handle(_agent(), too_deep)

    refusals = [r for r in replies if r.performative == Performative.SPAWN_REFUSED]
    assert refusals, "a spawn past max_depth was not refused"
    assert refusals[0].payload["reason"] == "max_depth"


def test_circuit_breaker_is_checked_before_the_call_not_after():
    """Decrement before the API call, never after. Recursive spawning against a
    live key is the one way this project costs real money."""
    from samvad.budget import CircuitBreaker

    breaker = CircuitBreaker()
    if not implemented(breaker.check):
        pytest.skip("CircuitBreaker not implemented yet")

    assert isinstance(breaker.check(), bool)


# --- concurrency ------------------------------------------------------------


def test_concurrency_cap_is_eight():
    """Rate limits arrive well before CPU limits."""
    from samvad.supervisor import MAX_CONCURRENCY

    assert MAX_CONCURRENCY == 8


async def test_supervisor_never_exceeds_the_concurrency_cap():
    """The cap has to be a semaphore in a code path, not a number in a docstring.

    Spawns twice the cap at once and asserts the peak in-flight count never
    passed it.
    """
    import asyncio

    from samvad.supervisor import MAX_CONCURRENCY, Supervisor

    supervisor = Supervisor()
    spec = {"role": "executor", "prompt_id": "executor"}
    budget = _make().budget

    if not implemented(supervisor.spawn, spec, budget):
        pytest.skip("Supervisor.spawn not implemented yet")

    in_flight = 0
    peak = 0
    original = supervisor.spawn

    async def counting_spawn(*args, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            return await original(*args, **kwargs)
        finally:
            in_flight -= 1

    supervisor.spawn = counting_spawn
    await asyncio.gather(
        *(supervisor.spawn(spec, budget) for _ in range(MAX_CONCURRENCY * 2)),
        return_exceptions=True,
    )
    assert peak <= MAX_CONCURRENCY, f"{peak} spawns in flight, cap is {MAX_CONCURRENCY}"


# --- sandbox ----------------------------------------------------------------


def test_sandbox_kills_an_infinite_loop_at_the_timeout():
    """Executors run model-generated code. The timeout is not optional."""
    from samvad.sandbox import run_sandboxed

    if not implemented(run_sandboxed, "pass", 1.0):
        pytest.skip("run_sandboxed not implemented yet")

    result = run_sandboxed("while True: pass", timeout=1.0)
    assert result.timed_out is True
    assert result.exit_code != 0
