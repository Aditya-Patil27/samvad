# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Reading what a small model writes, before `Agent` parses it.

P2's `Agent._parse` accepts a reply only when the whole text is one JSON object.
Small local models wrap that object in a fence, put a `<think>` block (qwen3) or
a line of prose in front of it, and the agent then reads the reply as prose,
which its arbiter defaults to `complete`. In measurement 5 that would score a
formatting slip as an accept, so the object is unwrapped first. Nothing is
invented: a reply that holds no such object passes through as text.
"""
from __future__ import annotations

import json
import re
from typing import Any

from samvad.llm.base import Completion

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```[ \t]*([\w+-]*)[^\n]*\n(.*?)```", re.DOTALL)


def json_object(text: str) -> dict[str, Any] | None:
    """The reply's JSON object, if it holds one with a field the agent reads.

    `strict=False` admits raw newlines inside strings, which is how a small
    model usually writes code into JSON.
    """
    stripped = _THINK.sub("", text).strip()
    candidates = [stripped, *(body for _, body in _FENCE.findall(stripped))]
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        candidates.append(stripped[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and ("task_status" in parsed or "result" in parsed):
            return parsed
    return None


def normalise_reply(text: str) -> str:
    """Strict JSON if the reply holds such an object, else the text minus `<think>`."""
    parsed = json_object(text)
    return json.dumps(parsed) if parsed is not None else _THINK.sub("", text).strip()


def extract_code(result: Any) -> str:
    """The source in an executor's `result`: a python fence first, any fence next."""
    text = result if isinstance(result, str) else str(result)
    blocks = _FENCE.findall(text)
    for lang, body in blocks:
        if lang.lower() in ("python", "py", "python3"):
            return body.strip()
    return (blocks[0][1] if blocks else text).strip()


class Normalising:
    """An LLM backend wrapper: hands `Agent` the normalised reply and keeps it."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.seen: list[str] = []

    async def complete(self, prompt: Any) -> Completion:
        completion = await self.inner.complete(prompt)
        self.seen.append(normalise_reply(completion.text))
        return Completion(text=self.seen[-1], usage=completion.usage)
