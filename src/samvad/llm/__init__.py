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

The paid backend is reachable only by naming it explicitly, and saying so
raises rather than silently starting to spend.
"""
from __future__ import annotations

import os
from typing import Any

OLLAMA_PREFIX = "ollama:"

#: Hosted providers, all OpenAI-compatible -- see llm/openai_compat.py.
HOSTED_PREFIXES = ("groq:", "nvidia:")

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
        "claude-opus-5" etc.  -> PaidBackendRefused

    The paid path raises with the cost stated rather than returning something
    that starts billing on its first call. This system spawns recursively; the
    failure mode of getting that wrong is not a wrong answer, it is a bill.
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

    raise PaidBackendRefused(
        f"{name!r} is a paid API model and this project is configured free-only.\n"
        f"  Free options:\n"
        "    model: mock                                no inference\n"
        "    model: ollama:qwen3:8b                     local inference\n"
        "    model: groq:llama-3.3-70b-versatile        hosted free tier\n"
        "    model: nvidia:meta/llama-3.1-70b-instruct  hosted free tier\n"
        f"  To use a paid model deliberately, implement samvad/llm/claude.py and\n"
        f"  set SAMVAD_ALLOW_PAID=1. It is not wired up by default on purpose."
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
