# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""LLM backend interface.

Completion MUST carry usage -- input, output and cache_read tokens plus the
model id. Every task_result reports its own cost; the dashboard sums them
live across the mesh.
"""
from dataclasses import dataclass
from typing import Protocol


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
