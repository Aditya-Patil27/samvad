"""Contract for measurement 5 (experiments/disagreement.py).

What these tests defend:

1. **Ground truth is the sandbox.** A task's code is good when the held-out
   tests exit 0 under `run_sandboxed`, and neither model decides that. Every
   task ships a known-good and a known-bad reference to prove its tests can
   tell them apart.
2. **The review is blind.** The reviewer sees the task and the code, never the
   exit code or the tests. A reviewer that saw them would be grading the
   sandbox, not the code, and the false-accept rate would measure nothing.
3. **Four cells, every rate.** Scripted replies land in each confusion cell, and
   each rate is checked against a hand count.
4. **Two verdicts.** The advisory verdict is the model's; the arbitrated one
   is what the runtime makes of the same reply once it has the exit code. With
   no exit code claimed by the reviewer, the arbiter leaves no false accept.
5. **Honest absence.** A model failure is an error row, not a reject; an empty
   denominator is an empty cell, not 0; a mock run says it is a mock.
"""
from __future__ import annotations

import csv
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from experiments import disagreement, replies
from samvad.llm.mock import MockBackend
from samvad.sandbox import run_sandboxed
from tests.reference_solutions import REFERENCES

GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"
TESTS = ["assert add(2, 3) == 5", "assert add(-1, 1) == 0"]
SAME = {"homo": ("mock:x", "mock:x")}


def _tasks_file(tmp_path: Path, n: int, name: str = "tasks.json") -> str:
    tasks = [{"id": f"s{i:02d}", "task": "Add two integers", "constraints": ["Python 3.11"],
              "function": "add", "signature": "add(a: int, b: int) -> int",
              "tests": TESTS} for i in range(n)]
    path = tmp_path / name
    path.write_text(json.dumps(tasks), encoding="utf-8")
    return str(path)


def _wrote(code: str, status: str = "complete") -> str:
    return json.dumps({"task_status": status, "result": code})


def _verdict(status: str, **extra: Any) -> str:
    return json.dumps({"task_status": status, "result": "reason", **extra})


class Factory:
    """Scripted backends per role; records every `(model, role)` it was asked for."""

    def __init__(self, executor: list[str], reviewer: list[str]) -> None:
        self.script = {"executor": executor, "reviewer": reviewer}
        self.calls: list[tuple[str, str]] = []
        self.made: dict[str, list[MockBackend]] = {"executor": [], "reviewer": []}

    def __call__(self, model: str, role: str) -> MockBackend:
        self.calls.append((model, role))
        backend = MockBackend(list(self.script[role]), model=model, simulate_cache=False)
        self.made[role].append(backend)
        return backend


async def _run(tmp_path: Path, factory: Factory, n: int, **kwargs: Any):
    kwargs.setdefault("pairs", ("homo",))
    kwargs.setdefault("models", SAME)
    return await disagreement.run(tasks_path=_tasks_file(tmp_path, n), min_tasks=1,
                                  backend_factory=factory, write=False, **kwargs)


# ==========================================================================
# the task set
# ==========================================================================

def test_every_task_carries_a_function_a_signature_and_held_out_tests():
    tasks = disagreement.load_tasks(None)
    assert len(tasks) >= 20, "docs/EXPERIMENTS.md: minimum 20 tasks per condition"
    assert len({t["id"] for t in tasks}) == len(tasks)
    for task in tasks:
        assert task["signature"].startswith(f"{task['function']}(")
        assert any(line.lstrip().startswith("assert ") for line in task["tests"])


def test_the_first_three_tasks_are_unchanged():
    """Measurement 1 replays t01-t03; they must not drift under it."""
    tasks = {t["id"]: t for t in disagreement.load_tasks(None)}
    assert (tasks["t01"]["task"], tasks["t01"]["constraints"]) == (
        "Implement a binary search function", ["O(log n)", "Python 3.11", "pytest"])
    assert (tasks["t02"]["task"], tasks["t02"]["constraints"]) == (
        "Write a function that flattens an arbitrarily nested list",
        ["no recursion limit issues", "pytest"])
    assert (tasks["t03"]["task"], tasks["t03"]["constraints"]) == (
        "Implement an LRU cache with a fixed capacity", ["O(1) get and put", "pytest"])


def test_the_tests_tell_a_good_reference_from_a_bad_one():
    """Good passes; bad runs cleanly on its own and fails only on the tests. A
    bad reference that crashed on import would prove nothing about the tests."""
    tasks = disagreement.load_tasks(None)
    assert set(REFERENCES) == {t["id"] for t in tasks}

    def exits(task: dict[str, Any]) -> tuple[str, int, int, int]:
        good, bad = REFERENCES[task["id"]]
        tests = "\n\n" + "\n".join(task["tests"]) + "\n"
        return (task["id"], run_sandboxed(good + tests).exit_code,
                run_sandboxed(bad).exit_code, run_sandboxed(bad + tests).exit_code)

    with ThreadPoolExecutor(8) as pool:
        for task_id, good, bad_alone, bad in pool.map(exits, tasks):
            assert good == 0, f"{task_id}: good reference fails its tests"
            assert bad_alone == 0, f"{task_id}: bad reference crashes before the tests"
            assert bad != 0, f"{task_id}: the tests do not catch the bad reference"


def test_a_task_without_tests_is_refused(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps([{"id": "x", "task": "t", "function": "f"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="tests"):
        disagreement.load_tasks(str(path))


# ==========================================================================
# the confusion matrix
# ==========================================================================

#: (code, executor status, reviewer verdict) -> advisory cell, arbitrated cell, disagree
SCRIPT = [
    (GOOD, "complete", "complete", "true_accept", "true_accept", 0),
    (GOOD, "uncertain", "complete", "true_accept", "true_accept", 1),
    (GOOD, "complete", "needs_revision", "false_reject", "false_reject", 1),
    (BAD, "complete", "complete", "false_accept", "catch", 0),
    (BAD, "complete", "complete", "false_accept", "catch", 0),
    (BAD, "uncertain", "needs_revision", "catch", "catch", 0),
]


async def test_scripted_replies_land_in_all_four_cells(tmp_path):
    factory = Factory([_wrote(c, s) for c, s, *_ in SCRIPT], [_verdict(v) for _, _, v, *_ in SCRIPT])
    rows, _ = await _run(tmp_path, factory, len(SCRIPT))
    assert [r["outcome"] for r in rows] == [s[3] for s in SCRIPT]
    assert [r["arbitrated_outcome"] for r in rows] == [s[4] for s in SCRIPT]
    assert [r["disagree"] for r in rows] == [s[5] for s in SCRIPT]
    assert [r["ground_truth"] for r in rows] == ["good"] * 3 + ["bad"] * 3
    assert all(r["error"] == "" and r["mock"] == 1 for r in rows)
    assert {r["executor_model"] for r in rows} == {r["reviewer_model"] for r in rows} == {"mock:x"}


async def test_every_rate_matches_a_hand_count(tmp_path):
    factory = Factory([_wrote(c, s) for c, s, *_ in SCRIPT], [_verdict(v) for _, _, v, *_ in SCRIPT])
    _, summary = await _run(tmp_path, factory, len(SCRIPT))
    row = summary[0]
    assert (row["n_tasks"], row["n_errors"], row["n_good"], row["n_bad"]) == (6, 0, 3, 3)
    assert (row["true_accept"], row["false_reject"], row["false_accept"], row["catch"]) == (
        2, 1, 2, 1)
    assert row["disagreement_rate"] == pytest.approx(2 / 6)
    assert row["catch_rate"] == pytest.approx(1 / 3)
    assert row["false_accept_rate"] == pytest.approx(2 / 3)
    assert row["false_reject_rate"] == pytest.approx(1 / 3)
    assert row["n_review_unparsed"] == 0
    assert row["review_ms_median"] != "" and row["review_ms_p95"] != ""


async def test_the_arbiter_leaves_no_false_accept_but_keeps_the_false_reject(tmp_path):
    """The runtime can veto `complete` on a failing exit code. It cannot rescue
    good code the reviewer rejected -- that is the reviewer's cost, not its."""
    factory = Factory([_wrote(c, s) for c, s, *_ in SCRIPT], [_verdict(v) for _, _, v, *_ in SCRIPT])
    _, summary = await _run(tmp_path, factory, len(SCRIPT))
    assert summary[0]["arbitrated_false_accept_rate"] == 0
    assert summary[0]["arbitrated_false_reject_rate"] == pytest.approx(1 / 3)


async def test_an_empty_denominator_is_left_empty_not_zero(tmp_path):
    factory = Factory([_wrote(GOOD)], [_verdict("complete")])
    _, summary = await _run(tmp_path, factory, 2)
    row = summary[0]
    assert row["n_bad"] == 0
    assert row["catch_rate"] == row["false_accept_rate"] == ""
    assert row["arbitrated_false_accept_rate"] == ""
    assert row["false_reject_rate"] == 0


# ==========================================================================
# blindness and what each role is told
# ==========================================================================

async def test_the_reviewer_sees_the_code_but_not_the_tests_or_the_exit_code(tmp_path):
    factory = Factory([_wrote(BAD)], [_verdict("complete")])
    await _run(tmp_path, factory, 2)
    reviewer, executor = factory.made["reviewer"][0], factory.made["executor"][0]
    assert reviewer.call_count == executor.call_count == 2
    for prompt in reviewer.prompts:
        payload = prompt["payload"]
        assert payload["code"] == BAD.strip()
        assert "exit_code" not in payload and "tests" not in payload
        assert not any(line in json.dumps(payload) for line in TESTS)
        assert prompt["root_task"] == "Add two integers", "root_task is copied verbatim"
    for prompt in executor.prompts:
        assert "tests" not in prompt["payload"]
        assert not any(line in json.dumps(prompt["payload"]) for line in TESTS)


async def test_fenced_and_think_wrapped_replies_are_read_not_defaulted(tmp_path):
    """P2's parser reads a fenced reply as prose, and prose defaults to `complete`."""
    fenced_code = "Here you go:\n```json\n" + _wrote(f"```python\n{GOOD}```") + "\n```"
    verdict = ("<think>It adds. Or does it?</think>\nVerdict:\n```json\n"
               + _verdict("needs_revision") + "\n```")
    rows, _ = await _run(tmp_path, Factory([fenced_code], [verdict]), 1)
    row = rows[0]
    assert (row["executor_parsed"], row["ground_truth"]) == (1, "good")
    assert (row["review_parsed"], row["review_status"]) == (1, "needs_revision")
    assert row["outcome"] == "false_reject"


async def test_a_prose_verdict_is_counted_as_unparsed(tmp_path):
    rows, summary = await _run(tmp_path, Factory([_wrote(BAD)], ["Looks right to me."]), 1)
    assert rows[0]["review_parsed"] == 0
    assert rows[0]["review_status"] == "complete", "P2's default for prose"
    assert summary[0]["n_review_unparsed"] == 1


async def test_a_reviewer_reported_exit_code_is_recorded(tmp_path):
    """P2's `_arbitrate` lets a model-reported exit_code outrank the runtime's,
    so a run must be able to show where a reviewer claimed one."""
    rows, _ = await _run(tmp_path, Factory([_wrote(BAD)], [_verdict("complete", exit_code=0)]), 1)
    assert rows[0]["review_claimed_exit_code"] == "0"


# ==========================================================================
# failures are recorded, never scored
# ==========================================================================

async def test_a_malformed_review_is_an_error_row_not_a_reject(tmp_path):
    malformed = [_verdict("needs_revision", claims=[{"bogus": 1}]),
                 json.dumps({"task_status": ["complete"], "result": "unhashable"}),
                 _verdict("needs_revision")]
    rows, summary = await _run(tmp_path, Factory([_wrote(BAD)], malformed), 3)
    assert [r["error"].split(":")[0] for r in rows] == ["reviewer", "reviewer", ""]
    assert rows[0]["outcome"] == rows[1]["outcome"] == ""
    assert (summary[0]["n_tasks"], summary[0]["n_errors"], summary[0]["catch"]) == (1, 2, 1)
    assert summary[0]["catch_rate"] == 1


async def test_a_failed_execution_is_not_reviewed(tmp_path):
    factory = Factory([json.dumps({"task_status": "complete", "result": GOOD,
                                   "claims": [{"bogus": 1}]}), _wrote(GOOD)],
                      [_verdict("complete")])
    rows, summary = await _run(tmp_path, factory, 2)
    assert rows[0]["error"].startswith("executor: ") and rows[0]["ground_truth"] == ""
    assert factory.made["reviewer"][0].call_count == 1
    assert (summary[0]["n_tasks"], summary[0]["n_errors"]) == (1, 1)


async def test_a_run_where_every_execution_fails_is_refused(tmp_path):
    broken = json.dumps({"task_status": "complete", "result": "x", "claims": [{"bogus": 1}]})
    with pytest.raises(RuntimeError, match="every executor call"):
        await _run(tmp_path, Factory([broken], [_verdict("complete")]), 2)


# ==========================================================================
# pairs
# ==========================================================================

async def test_both_pairs_review_one_execution_of_each_task(tmp_path):
    """Paired design: homo and hetero judge byte-identical code."""
    factory = Factory([_wrote(GOOD), _wrote(BAD)], [_verdict("complete")])
    rows, summary = await _run(tmp_path, factory, 4, pairs=disagreement.CONDITIONS, models=None)
    (executor, homo), (_, hetero) = disagreement.DEFAULT_MODELS.values()
    assert factory.calls == [(executor, "executor"), (homo, "reviewer"), (hetero, "reviewer")]
    assert factory.made["executor"][0].call_count == 4
    assert [b.call_count for b in factory.made["reviewer"]] == [4, 4]
    by_task: dict[str, set[str]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], set()).add(row["code_sha256"])
    assert all(len(hashes) == 1 for hashes in by_task.values())
    assert [s["condition"] for s in summary] == ["homo", "hetero"]
    assert [s["reviewer"] for s in summary] == [homo, hetero]


def test_a_pair_must_be_labelled_honestly():
    with pytest.raises(ValueError, match="mislabelled"):
        disagreement.Pair("homo", "a", "b")
    with pytest.raises(ValueError, match="mislabelled"):
        disagreement.Pair("hetero", "a", "a")
    with pytest.raises(ValueError, match="unknown pair"):
        disagreement.make_pairs(["triad"])
    with pytest.raises(ValueError, match="duplicate"):
        disagreement.make_pairs(["homo", "homo"])
    assert disagreement.make_pairs(["hetero"], {"hetero": ("a", "b")})[0].reviewer == "b"


def test_the_summary_refuses_an_unknown_pair():
    with pytest.raises(ValueError, match="unknown pair"):
        disagreement.summarize_pairs([{"condition": "triad"}])


# ==========================================================================
# reading replies
# ==========================================================================

def test_normalise_reply_unwraps_json_and_invents_nothing():
    wrapped = '<think>hm</think>\nSure! {"task_status": "complete", "result": "a\nb"} done'
    assert json.loads(replies.normalise_reply(wrapped)) == {"task_status": "complete",
                                                            "result": "a\nb"}
    assert replies.normalise_reply("<think>x</think> just prose") == "just prose"
    assert replies.normalise_reply('{"unrelated": 1}') == '{"unrelated": 1}'


def test_extract_code_prefers_a_python_fence():
    text = "```text\nnot this\n```\n```python\ndef f():\n    return 1\n```"
    assert replies.extract_code(text) == "def f():\n    return 1"
    assert replies.extract_code("```\nx = 1\n```") == "x = 1"
    assert replies.extract_code("  x = 2\n") == "x = 2"


# ==========================================================================
# output
# ==========================================================================

async def test_mock_llm_selects_the_mock_and_says_so_in_the_csv(tmp_path):
    """conftest sets MOCK_LLM=1, so the default factory must not reach Ollama."""
    rows, summary = await disagreement.run(tasks_path=_tasks_file(tmp_path, 2), min_tasks=1,
                                           results_dir=str(tmp_path / "out"))
    assert len(rows) == 4 and all(r["mock"] == 1 for r in rows)
    text = (tmp_path / "out" / "disagreement_summary.csv").read_text(encoding="utf-8")
    written = list(csv.DictReader(text.splitlines()))
    assert [r["condition"] for r in written] == ["homo", "hetero"]
    assert all(r["mock"] == "1" and "mock[" in r["run_models"] for r in written)
    header = (tmp_path / "out" / "disagreement_tasks.csv").read_text(encoding="utf-8")
    for column in ("run_git_sha", "run_network", "outcome", "arbitrated_outcome", "code_sha256"):
        assert column in header.splitlines()[0]
    assert summary[0]["n_tasks"] == 2


async def test_a_run_below_the_doc_floor_is_refused(tmp_path):
    """docs/EXPERIMENTS.md: 'Minimum 20 tasks per condition.'"""
    with pytest.raises(ValueError, match="20"):
        await disagreement.run(tasks_path=_tasks_file(tmp_path, 3), write=False)


def test_the_cli_writes_both_csvs_and_labels_a_mock_run(tmp_path, capsys):
    """Takes the harness's shared switches, as `python -m experiments` passes them."""
    out = tmp_path / "out"
    code = disagreement.main(["--seed", "7", "--tasks", _tasks_file(tmp_path, 2),
                              "--min-tasks", "1", "--results-dir", str(out)])
    assert code == 0
    assert (out / "disagreement_tasks.csv").exists()
    assert (out / "disagreement_summary.csv").exists()
    assert "MOCK" in capsys.readouterr().err


def test_the_cli_refuses_a_malformed_or_mislabelled_pair(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        disagreement.main(["--homo", "only-one-model"])
    assert exc.value.code == 2
    code = disagreement.main(["--tasks", _tasks_file(tmp_path, 2), "--min-tasks", "1",
                              "--homo", "a,b", "--results-dir", str(tmp_path)])
    assert code == 2 and "mislabelled" in capsys.readouterr().err
