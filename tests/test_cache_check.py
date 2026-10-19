"""scripts/cache_check.py -- the offline checks, and --live against a fake client.

Nothing here has a key or touches the network: the live path gets a
ClaudeBackend whose client is a stub with an async `messages.create`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from samvad.llm.claude import ClaudeBackend

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cache_check.py"
_spec = importlib.util.spec_from_file_location("cache_check", SCRIPT)
assert _spec is not None and _spec.loader is not None
cache_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cache_check)

LONG = "You are the executor. Implement exactly the subtask given.\n" * 60  # ~3500 chars


def chat(system: str, user: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --- offline -----------------------------------------------------------------

def test_a_plain_prompt_has_no_breakpoint() -> None:
    [problem] = cache_check.check({"root_task": "x", "turn": 1}, {"root_task": "x", "turn": 2})
    assert "no cache breakpoint" in problem


def test_a_stable_long_prefix_passes() -> None:
    assert cache_check.check(chat(LONG, "turn 1"), chat(LONG, "turn 3")) == []


def test_a_changing_prefix_is_located() -> None:
    first = chat(LONG + "sent 2026-10-19T10:00:00Z", "turn 1")
    second = chat(LONG + "sent 2026-10-19T10:00:07Z", "turn 3")
    [problem] = cache_check.check(first, second)
    assert "changes between turns" in problem
    assert "10:00:00" in problem and "10:00:07" in problem


def test_the_minimum_is_per_model() -> None:
    prompts = chat(LONG, "turn 1"), chat(LONG, "turn 3")
    assert cache_check.check(*prompts, model="claude-opus-5") == []
    [problem] = cache_check.check(*prompts, model="claude-haiku-4-5")
    assert "under claude-haiku-4-5's minimum of 4096" in problem


def test_the_role_prompt_alone_is_under_every_minimum() -> None:
    role = (Path(cache_check.__file__).parent.parent / "src" / "samvad" / "prompts"
            / "executor.md").read_text(encoding="utf-8")
    for model in cache_check.MIN_CACHEABLE_TOKENS:
        [problem] = cache_check.check(*cache_check.agent_prompts(role), model=model)
        assert "under" in problem


def test_agent_prompts_come_from_the_agent_and_share_root_task() -> None:
    first, second = cache_check.agent_prompts()
    assert first["root_task"] == second["root_task"] == cache_check.ROOT_TASK
    assert first["turn"] != second["turn"]


def test_a_system_file_goes_above_the_agent_prompt(tmp_path: Path) -> None:
    (tmp_path / "s.md").write_text(LONG, encoding="utf-8")
    assert cache_check.main(["--system-file", str(tmp_path / "s.md")]) == 0
    (tmp_path / "short.md").write_text("Be brief.", encoding="utf-8")
    assert cache_check.main(["--system-file", str(tmp_path / "short.md")]) == 1


# --- --live never spends on a known zero or under the locks -------------------

@pytest.fixture
def no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a backend was built")

    monkeypatch.setattr(cache_check, "backend_for", refuse)


def test_live_does_not_send_a_prompt_that_cannot_cache(
    no_backend: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MOCK_LLM", "0")
    assert cache_check.main(["--live"]) == 1
    assert "Not sending" in capsys.readouterr().out


def test_live_does_not_send_under_mock_llm(
    no_backend: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_LLM", "1")
    (tmp_path / "s.md").write_text(LONG, encoding="utf-8")
    assert cache_check.main(["--system-file", str(tmp_path / "s.md"), "--live"]) == 1


def test_live_respects_the_paid_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_LLM", "0")
    monkeypatch.delenv("SAMVAD_ALLOW_PAID", raising=False)
    (tmp_path / "s.md").write_text(LONG, encoding="utf-8")
    assert cache_check.main(["--system-file", str(tmp_path / "s.md"), "--live"]) == 1


# --- --live, against a fake API ----------------------------------------------

class FakeMessages:
    """Answers each call with the next (cache_read, stop_reason)."""

    def __init__(self, script: list[tuple[int, str]]) -> None:
        self.script = script
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        cache_read, stop_reason = self.script[len(self.requests) - 1]
        return {
            "stop_reason": stop_reason,
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 40, "output_tokens": 12,
                      "cache_creation_input_tokens": 0 if cache_read else 1200,
                      "cache_read_input_tokens": cache_read},
        }


def run_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
             script: list[tuple[int, str]]) -> tuple[int, FakeMessages]:
    messages = FakeMessages(script)
    client = type("Client", (), {"messages": messages})()
    monkeypatch.setenv("MOCK_LLM", "0")
    monkeypatch.setattr(cache_check, "backend_for",
                        lambda model, **kw: ClaudeBackend(model=model, client=client, **kw))
    (tmp_path / "s.md").write_text(LONG, encoding="utf-8")
    return cache_check.main(["--system-file", str(tmp_path / "s.md"), "--live"]), messages


def test_live_passes_when_the_second_call_reads_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, messages = run_live(monkeypatch, tmp_path, [(0, "end_turn"), (1200, "end_turn")])
    assert code == 0
    assert "PASS" in capsys.readouterr().out
    assert [r["max_tokens"] for r in messages.requests] == [cache_check.LIVE_MAX_TOKENS] * 2
    assert messages.requests[0]["system"] == messages.requests[1]["system"]


def test_live_fails_when_it_reads_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_live(monkeypatch, tmp_path, [(0, "end_turn"), (0, "end_turn")])
    assert code == 1
    assert "read nothing from cache" in capsys.readouterr().out


def test_a_call_cut_off_at_max_tokens_still_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, _ = run_live(monkeypatch, tmp_path, [(0, "max_tokens"), (1200, "max_tokens")])
    assert code == 0
