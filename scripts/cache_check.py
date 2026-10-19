# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Verify prompt caching is actually hitting.

Caching is a PREFIX match: any byte change anywhere in the prefix invalidates
everything after it. If cache_read_input_tokens is zero across repeated calls,
something in your prefix is changing.

Usual suspects: a timestamp or message id in the system prompt, unsorted JSON
in a serialised tool definition, a varying tool list, or a prefix under the
model's minimum (below it the breakpoint is silently ignored).

    python scripts/cache_check.py                          # offline, nothing sent
    python scripts/cache_check.py --system-file draft.md   # ...with a system turn of yours
    MOCK_LLM=0 SAMVAD_ALLOW_PAID=1 python scripts/cache_check.py --live

Offline, it builds two consecutive turns of one conversation with the agent's
own `_prompt`, turns each into the request llm/claude.py would send, and checks
what can be checked without the API: is there a breakpoint, is everything above
it byte-identical across the two turns, and is it over the model's minimum.
`--system-file` puts a system turn of your own above the agent's prompt, to
check the plumbing before the agent sends one.

--live sends those two requests and reads usage.cache_read_input_tokens off the
second. It refuses to spend anything when the offline check already says the
answer is zero. Manual only: never in CI, never under MOCK_LLM=1, and it needs
ANTHROPIC_API_KEY in the environment.

Exit code 0 when caching will hit (offline) or did hit (--live), 1 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from samvad.agent import Agent, usd_for
from samvad.llm import PaidBackendRefused, backend_for
from samvad.llm.base import Usage, render_prompt
from samvad.llm.claude import DEFAULT_MODEL, RATES_USD_PER_MTOK, ClaudeError, _request

#: Below these the breakpoint is ignored, with no error. From llm/claude.py.
MIN_CACHEABLE_TOKENS: dict[str, int] = {
    "claude-opus-5": 512,
    "claude-sonnet-5": 1024,
    "claude-haiku-4-5": 4096,
}

#: For an estimate only. Generous -- prose runs nearer four characters a token --
#: so a prefix this calls short is short, and one it passes may still not be:
#: --live is the arbiter.
CHARS_PER_TOKEN = 3

#: Output cap per live call. Only usage is read, and thinking counts against
#: the cap, so a call cut off here is still a valid measurement.
LIVE_MAX_TOKENS = 512

ROOT_TASK = "Implement a binary search function"  # experiments/tasks.json, t01


def _turn(turn: int, performative: str, payload: dict[str, Any]) -> SimpleNamespace:
    """The fields Agent._prompt reads off a Message, and no others."""
    return SimpleNamespace(root_task=ROOT_TASK, performative=performative,
                           payload=payload, artifacts=[], turn=turn)


TURNS = (
    _turn(1, "task_request", {"subtask": "binary_search(xs, target) -> index or -1"}),
    _turn(3, "task_result", {"exit_code": 1, "stdout": "1 failed: test_empty_list"}),
)


def agent_prompts(system: str | None = None) -> list[Any]:
    """Two consecutive turns, prompted by the agent's own code.

    `Agent._prompt` uses nothing of the agent, so it is called unbound on
    stand-ins. If it starts needing more, this fails loudly, which is the point:
    a copy of it here would go on passing after the real one changed.
    """
    prompts = [Agent._prompt(None, t) for t in TURNS]  # type: ignore[arg-type]
    if system is None:
        return prompts
    return [[{"role": "system", "content": system},
             {"role": "user", "content": render_prompt(p)}] for p in prompts]


def cached_prefix(request: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The system blocks up to the last breakpoint, or None when there is none.

    Render order is tools -> system -> messages, nothing here sends tools, and
    llm/claude.py puts its one breakpoint on the last system block.
    """
    system = request.get("system") or []
    marked = [i for i, block in enumerate(system) if "cache_control" in block]
    return system[: marked[-1] + 1] if marked else None


def _first_difference(a: str, b: str) -> int:
    return next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))


def check(first: Any, second: Any, model: str = DEFAULT_MODEL) -> list[str]:
    """Why caching cannot hit across these two prompts; empty when it can."""
    a, b = cached_prefix(_request(first)), cached_prefix(_request(second))
    if a is None:
        return [("no cache breakpoint: the prompt has no system turn, so llm/claude.py "
                 "marks nothing and every call bills its full input")]

    problems = []
    a_bytes = json.dumps(a, ensure_ascii=False)
    b_bytes = json.dumps(b, ensure_ascii=False)
    if a_bytes != b_bytes:
        at = _first_difference(a_bytes, b_bytes)
        problems.append(f"the prefix changes between turns at character {at}: "
                        f"{a_bytes[max(0, at - 30):at + 30]!r} then "
                        f"{b_bytes[max(0, at - 30):at + 30]!r}")

    tokens = estimate_tokens(a)
    minimum = MIN_CACHEABLE_TOKENS.get(model, 0)
    if tokens < minimum:
        problems.append(f"the prefix is ~{tokens} tokens, under {model}'s minimum of "
                        f"{minimum}, so the breakpoint is ignored")
    return problems


def estimate_tokens(blocks: Sequence[dict[str, Any]]) -> int:
    return sum(len(block["text"]) for block in blocks) // CHARS_PER_TOKEN


async def live(prompts: Sequence[Any], model: str) -> int:
    """Two paid calls, in order; pass when the second reads from cache."""
    backend = backend_for(model, max_tokens=LIVE_MAX_TOKENS)
    usage: Usage | None = None
    try:
        for n, prompt in enumerate(prompts, 1):
            try:
                usage = (await backend.complete(prompt)).usage
            except ClaudeError as exc:
                if exc.usage is None:
                    raise
                usage = exc.usage  # cut off or refused, but billed, and usage is all we read
            print(f"call {n}: input {usage.input:>6}  cache_read {usage.cache_read:>6}  "
                  f"output {usage.output:>5}  budget ${usd_for(usage):.4f}")
    except ClaudeError as exc:
        print(f"FAIL: {exc}")
        return 1
    finally:
        await backend.aclose()

    if usage is not None and usage.cache_read > 0:
        print(f"PASS: the second call read {usage.cache_read} tokens from cache")
        return 0
    print("FAIL: the second call read nothing from cache, though the prefix sent was "
          "byte-identical. Most likely it is under the minimum after all: the offline "
          "token count is an estimate. Try --system-file with a longer prefix.")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cache_check", description=__doc__.split("\n")[0])
    parser.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(RATES_USD_PER_MTOK))
    parser.add_argument("--system-file", type=Path, default=None,
                        help="a system turn to put above the agent's prompt")
    parser.add_argument("--live", action="store_true",
                        help="two paid calls; needs MOCK_LLM=0 and SAMVAD_ALLOW_PAID=1")
    args = parser.parse_args(argv)

    system = args.system_file.read_text(encoding="utf-8") if args.system_file else None
    prompts = agent_prompts(system)
    source = f"the agent's prompt under {args.system_file}" if system else "the agent's prompt"

    problems = check(*prompts, model=args.model)
    if problems:
        print(f"FAIL: {source} will not cache on {args.model}")
        for problem in problems:
            print(f"  - {problem}")
        if args.live:
            print("Not sending: two paid calls would only confirm the zero.")
        return 1

    tokens = estimate_tokens(cached_prefix(_request(prompts[0])) or [])
    print(f"ok: {source} has a ~{tokens} token prefix, byte-identical across turns")
    if not args.live:
        print("Offline only. --live sends both turns and reads cache_read off the second.")
        return 0

    if os.environ.get("MOCK_LLM", "1") != "0":
        print("Not sending: MOCK_LLM is not 0. Run with MOCK_LLM=0 SAMVAD_ALLOW_PAID=1.")
        return 1
    try:
        return asyncio.run(live(prompts, args.model))
    except PaidBackendRefused as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
