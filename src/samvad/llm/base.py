# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""LLM backend interface.

Completion MUST carry usage -- input, output and cache_read tokens plus the
model id. Every task_result reports its own cost; the dashboard sums them
live across the mesh.
"""
import json
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Usage:
    input: int
    output: int
    cache_read: int
    model: str


@dataclass
class Completion:
    text: str
    usage: Usage


class LLMBackend(Protocol):
    async def complete(self, prompt) -> Completion: ...


def render_prompt(prompt: Any) -> str:
    """A stable string for any prompt shape. ONE definition, used by every backend.

    Two requirements, and they are why this is not per-backend:

    STABLE -- a default `repr()` carries a memory address, which would make
    token counts differ between runs of the same prompt and quietly corrupt
    measurement 4.

    IDENTICAL ACROSS BACKENDS -- measurement 5 compares a Claude reviewer
    against an Ollama one and asks whether their errors correlate. That answer
    only means something if both models received the same bytes. Two copies of
    this function would drift, and the comparison would stop being like-for-like
    without anything failing to warn you.

    P2 has not settled the prompt type yet, and this must not be the thing that
    forces the decision -- hence the shape-agnostic handling.
    """
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, bytes):
        return prompt.decode("utf-8", "replace")
    try:
        return json.dumps(prompt, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return repr(prompt)
