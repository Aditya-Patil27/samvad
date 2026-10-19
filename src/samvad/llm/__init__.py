# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""Backend selection from a peer's `model` string.

config/peers.yaml names a model per peer, and until now nothing read it: every
node got the mock regardless of what the table said. This is what makes the
config mean something.

THIS PROJECT RUNS FREE. Two backends cost nothing and both are real:

    mock              deterministic canned replies, no inference at all
    ollama:<model>    a model running locally on the device
    groq:<model>      hosted, free tier, OpenAI-compatible
    nvidia:<model>    hosted, free tier, OpenAI-compatible

Hosted free tiers matter for measurement 5: it needs an executor and reviewer
with UNCORRELATED training priors, and a 6GB card holding a 6GB model has no
room for a second one. A hosted reviewer removes the VRAM constraint and is
far faster than a laptop GPU.

The paid backend (claude-*, llm/claude.py) is behind two locks: MOCK_LLM=0, or
default_backend never looks at the model at all, and SAMVAD_ALLOW_PAID=1, or
naming a claude model raises with its price instead of starting to spend.
"""
from __future__ import annotations

import os
from typing import Any

OLLAMA_PREFIX = "ollama:"

#: Hosted providers, all OpenAI-compatible -- see llm/openai_compat.py.
HOSTED_PREFIXES = ("groq:", "nvidia:")

#: Paid. Routed only when ALLOW_PAID_ENV is exactly "1" -- see llm/claude.py.
CLAUDE_PREFIX = "claude-"
ALLOW_PAID_ENV = "SAMVAD_ALLOW_PAID"

_FREE_OPTIONS = (
    "  Free options:\n"
    "    model: mock                                no inference\n"
    "    model: ollama:qwen3:8b                     local inference\n"
    "    model: groq:llama-3.3-70b-versatile        hosted free tier\n"
    "    model: nvidia:meta/llama-3.1-70b-instruct  hosted free tier\n"
)

#: Names that mean "no inference". Anything falsy lands here too, so a peer
#: entry with no model at all runs the mock instead of failing at startup.
MOCK_NAMES = frozenset({"mock", "mock_llm", "none", "test"})


class PaidBackendRefused(RuntimeError):
    """A paid model was requested. Raised instead of quietly spending."""


def backend_for(model: str | None, **kwargs: Any) -> Any:
    """Build the backend a peer's `model` string names.

    Routing:
        ""  / "mock"          -> MockBackend      free, no inference
        "ollama:qwen3:8b"     -> OllamaBackend           free, local
        "groq:<model>"        -> OpenAICompatBackend     free tier, hosted
        "nvidia:<model>"      -> OpenAICompatBackend     free tier, hosted
        "claude-opus-5" etc.  -> ClaudeBackend           paid, SAMVAD_ALLOW_PAID=1 only
        anything else         -> PaidBackendRefused

    Without the opt-in the paid path raises with the cost stated rather than
    returning something that starts billing on its first call. This system
    spawns recursively; the failure mode of getting that wrong is not a wrong
    answer, it is a bill. The opt-in is exactly "1": a typo stays free.
    """
    name = (model or "").strip()

    if not name or name.lower() in MOCK_NAMES:
        from samvad.llm.mock import MockBackend

        return MockBackend(**kwargs)

    if name.lower().startswith(OLLAMA_PREFIX):
        from samvad.llm.ollama import OllamaBackend

        return OllamaBackend(model=name[len(OLLAMA_PREFIX):] or None, **kwargs)

    if name.lower().startswith(HOSTED_PREFIXES):
        from samvad.llm.openai_compat import OpenAICompatBackend

        return OpenAICompatBackend(model=name, **kwargs)

    if name.lower().startswith(CLAUDE_PREFIX):
        from samvad.llm.claude import RATES_USD_PER_MTOK, ClaudeBackend

        if os.environ.get(ALLOW_PAID_ENV) == "1":
            return ClaudeBackend(model=name, **kwargs)

        rate_in, rate_out = RATES_USD_PER_MTOK.get(name, (None, None))
        price = (
            f"${rate_in:.2f} in / ${rate_out:.2f} out per MTok"
            if rate_in is not None
            else "no published rate in llm/claude.py"
        )
        raise PaidBackendRefused(
            f"{name!r} is a paid API model ({price}) and paid models are off.\n"
            f"{_FREE_OPTIONS}"
            f"  To use it deliberately: MOCK_LLM=0, {ALLOW_PAID_ENV}=1, and\n"
            f"  ANTHROPIC_API_KEY in .env. It is opt-in on purpose."
        )

    raise PaidBackendRefused(
        f"{name!r} is not a model this project has a backend for, and anything\n"
        f"unrecognised is treated as paid.\n"
        f"{_FREE_OPTIONS}"
        f"  The only paid backend is claude-* (llm/claude.py, {ALLOW_PAID_ENV}=1)."
    )


def default_backend(model: str | None = None, **kwargs: Any) -> Any:
    """The backend for this process, honouring MOCK_LLM.

    MOCK_LLM=1 is the default and it WINS over the peer table. A config that
    names a real model still runs the mock unless someone explicitly turns
    MOCK_LLM off -- so a stray `model:` entry can never start inference during
    a test run.
    """
    if os.environ.get("MOCK_LLM", "1") != "0":
        from samvad.llm.mock import MockBackend

        return MockBackend(**kwargs)
    return backend_for(model, **kwargs)
