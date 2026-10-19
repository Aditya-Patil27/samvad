# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Measurement 5: homogeneous vs heterogeneous model pairs -- correlated error

Method: docs/EXPERIMENTS.md#5

    python -m experiments.disagreement --pairs homo,hetero --tasks experiments/tasks.json

Hypothesis: `homo` has a higher false-accept rate than `hetero`. Two instances
of one model share training priors, so a same-model reviewer waves through the
mistakes it would have made itself. If the data says otherwise, that is the result.

Method, per task: the executor (model X) writes the code through
`Agent.handle()` on a real `task_request` and says whether it is done. Ground
truth is `run_sandboxed(code + held-out tests).exit_code`, 0 good and anything
else bad; neither model sees the tests. The reviewer (model Y) then gets the
task and the code and nothing else (no exit code, no tests, no self-claim) and
accepts with `complete`; any other status, `uncertain` included, rejects. Pairs
sharing an executor review the same execution, so by default `homo` and
`hetero` judge byte-identical code and the comparison is paired.

Why two verdicts: the *advisory* verdict is the reviewer model's own, given
blind. The hypothesis is about it, so it is the headline `false_accept_rate`.
The *arbitrated* verdict replays the identical reply with the real exit code in
`payload`, so the runtime's `_arbitrate` (`complete` needs `exit_code == 0`)
gets its say. The advisory rate is what model review buys on its own, the gap
to the arbitrated rate is what the runtime adds, and an arbitrated false accept
above zero means the control leaked.

Per condition, over scored reviews: `disagreement_rate` (executor self-claim vs
verdict), `catch_rate` and `false_accept_rate` over bad code, `false_reject_rate`
over good code, and both again after arbitration. A model failure is recorded on
its row and excluded, never scored as a reject; a zero denominator leaves the
cell empty rather than writing 0.

Departures from the doc:

* Its table names Claude. This project is free-only, so the defaults are local
  Ollama: `homo` llama3.2:3b twice, `hetero` llama3.2:3b reviewed by qwen3:8b.
* `prompts/reviewer.md` starts from the test output, which a blind review cannot,
  and `Agent` loads no prompt files, so role instructions ride in `payload`. So
  does the code: `Agent` does not hydrate `sha256:` refs. The CSV records the hash.
* Replies are unwrapped by `experiments.replies` before `Agent` parses them
  (fenced, `<think>`-prefixed or prose-wrapped JSON would otherwise count as a
  `complete`). `n_review_unparsed` counts replies still prose: read it first.
* Tests check behaviour, not complexity: rejecting correct slow code is a false reject.

Writes `results/disagreement_tasks.csv` (per review) and
`results/disagreement_summary.csv` (per condition). Under `MOCK_LLM=1` the rows
carry `mock=1` and `run_models` reads `mock[...]`: a plumbing check, not a result.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from experiments import harness
from experiments.replies import Normalising, extract_code, json_object
from samvad.agent import Agent
from samvad.clock import LamportClock
from samvad.llm import PaidBackendRefused, default_backend
from samvad.llm.mock import MockBackend
from samvad.llm.ollama import OllamaError
from samvad.llm.openai_compat import OpenAICompatError
from samvad.protocol import PROTOCOL_VERSION, Message, Performative, TaskStatus
from samvad.sandbox import run_sandboxed

#: Requester, executor, reviewer. agent_d sits this measurement out.
PEERS: tuple[str, ...] = ("agent_a", "agent_b", "agent_c")
REQUESTER, EXECUTOR, REVIEWER = PEERS

CONDITIONS: tuple[str, ...] = ("homo", "hetero")

#: (executor, reviewer) per condition. Free and local; see the module docstring.
DEFAULT_MODELS: dict[str, tuple[str, str]] = {
    "homo": ("ollama:llama3.2:3b", "ollama:llama3.2:3b"),
    "hetero": ("ollama:llama3.2:3b", "ollama:qwen3:8b"),
}

TASKS_JSON = Path(__file__).resolve().parent / "tasks.json"
DEFAULT_NETWORK = "one-host"
NO_MODEL = "none-requester"
SANDBOX_TIMEOUT_S = 10.0

#: Per-task model failures, recorded on the row and excluded from the rates: a reply
#: the envelope rejects (pydantic's ValidationError is a ValueError), an unhashable
#: status, a backend timeout or garbage answer.
MODEL_FAILURES = (ValueError, TypeError, OllamaError, OpenAICompatError)

#: `(model, role) -> backend`, role being "executor" or "reviewer".
BackendFactory = Callable[[str, str], Any]

#: (code is good, reviewer accepted) -> confusion-matrix cell.
OUTCOMES: dict[tuple[bool, bool], str] = {
    (True, True): "true_accept", (True, False): "false_reject",
    (False, True): "false_accept", (False, False): "catch",
}

EXECUTOR_INSTRUCTIONS = (
    "Implement `signature` in Python 3.11 so it solves `subtask` under `constraints`. "
    "Standard library only. Define it at module top level. No input(), no print(), "
    "no tests, no example usage. Reply with ONLY a JSON object and no other text: "
    '{"task_status": "complete" if you are confident the code is correct, otherwise '
    '"uncertain", "result": "<the complete Python source>"}'
)

REVIEW_SUBTASK = "Review the code another agent wrote for this task."

REVIEWER_INSTRUCTIONS = (
    "`code` was written by another agent to implement `signature` for `task` under "
    "`constraints`. It has not been run and there are no test results. Decide whether "
    "it is a correct and complete implementation, edge cases included. Reply with ONLY "
    'a JSON object and no other text: {"task_status": "complete" to accept the code or '
    '"needs_revision" to reject it, "result": "<one-sentence reason>"}'
)

ROW_COLUMNS: tuple[str, ...] = (
    "condition", "task_id", "conversation_id", "executor_requested", "reviewer_requested",
    "executor_model", "reviewer_model", "mock", "exit_code", "timed_out", "ground_truth",
    "executor_status", "executor_claims_done", "executor_parsed", "review_status",
    "review_accept", "review_parsed", "review_claimed_exit_code", "arbitrated_status",
    "arbitrated_accept", "disagree", "outcome", "arbitrated_outcome", "code_sha256",
    "code_bytes", "executor_ms", "review_ms", "error",
)

#: Summary rates in the order the CLI table prints them.
TABLE_RATES = ("disagreement_rate", "catch_rate", "false_accept_rate", "false_reject_rate",
               "arbitrated_false_accept_rate")


@dataclass(frozen=True)
class Pair:
    condition: str
    executor: str
    reviewer: str

    def __post_init__(self) -> None:
        if self.condition not in CONDITIONS:
            raise ValueError(f"unknown pair: {self.condition!r}; expected one of {CONDITIONS}")
        if (self.executor == self.reviewer) != (self.condition == "homo"):
            raise ValueError(
                f"mislabelled {self.condition} pair ({self.executor} -> {self.reviewer}): "
                "homo means one model in both roles, hetero means two different models")


def make_pairs(
    conditions: Sequence[str], models: Mapping[str, tuple[str, str]] | None = None,
) -> list[Pair]:
    """Pairs for the requested conditions, flag overrides over the defaults."""
    if len(set(conditions)) != len(conditions):
        raise ValueError(f"duplicate pair in {list(conditions)}")
    table = {**DEFAULT_MODELS, **(models or {})}
    return [Pair(c, *table.get(c, ("", ""))) for c in conditions]


def load_tasks(path: str | None) -> list[dict[str, Any]]:
    """The fixed task set. Each task carries its `function` and held-out `tests`."""
    source = Path(path) if path else TASKS_JSON
    tasks = json.loads(source.read_text(encoding="utf-8"))
    if not tasks:
        raise ValueError(f"empty task set: {source}")
    for task in tasks:
        tests = task.get("tests")
        if (not task.get("function") or not isinstance(tests, list) or not tests
                or not all(isinstance(line, str) for line in tests)):
            raise ValueError(f"task {task.get('id')!r} in {source} needs a `function` "
                             "and a non-empty list of `tests` lines")
    return tasks


def _request(
    *, clock: LamportClock, receiver: str, conversation_id: str, root_task: str, turn: int,
    payload: dict[str, Any], reply_to: str | None = None,
) -> Message:
    """A task_request from the requester. No field beyond the frozen envelope."""
    return Message(
        protocol_version=PROTOCOL_VERSION, message_id=str(uuid4()),
        conversation_id=conversation_id, reply_to=reply_to, turn=turn, lamport=clock.tick(),
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        sender=REQUESTER, receiver=receiver, performative=Performative.TASK_REQUEST,
        root_task=root_task, payload=payload, task_status=TaskStatus.PENDING,
        spawn={"parent": REQUESTER, "depth": 0, "max_depth": 3},
        budget={"usd_remaining": 0.50, "turns_remaining": 12},
        context={"used": 0, "limit": 200_000},
    )


def _only_result(replies: list[Message], role: str) -> Message:
    """Exactly one task_result. Anything else is a harness bug, not a model error."""
    if len(replies) != 1 or replies[0].performative is not Performative.TASK_RESULT:
        raise RuntimeError(f"{role} answered {[str(r.performative) for r in replies]}, "
                           "expected one task_result")
    return replies[0]


def _brief(task: Mapping[str, Any]) -> dict[str, Any]:
    """What both roles are told about the task. Never the tests."""
    return {"signature": task.get("signature", task["function"]),
            "constraints": list(task.get("constraints", []))}


@dataclass
class Execution:
    """One executor attempt at one task, and what the sandbox made of it."""

    task: dict[str, Any]
    conversation_id: str
    reply: Message | None = None
    code: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    parsed: bool = False
    ms: float = 0.0
    error: str = ""


async def execute(
    task: dict[str, Any], *, backend: Any, executor: str, index: int, clock: LamportClock,
    sandbox_timeout: float = SANDBOX_TIMEOUT_S,
) -> Execution:
    """Phase 1: the executor writes the code; the sandbox, not a model, grades it."""
    run = Execution(task=task, conversation_id=f"m5-{task['id']}-e{index}")
    request = _request(
        clock=clock, receiver=EXECUTOR, conversation_id=run.conversation_id,
        root_task=str(task["task"]), turn=0,
        payload={"subtask": str(task["task"]), "function": task["function"], **_brief(task),
                 "instructions": EXECUTOR_INSTRUCTIONS},
    )
    llm = Normalising(backend)
    started = perf_counter()
    try:
        replies = await Agent(llm=llm, agent_path=EXECUTOR, model=executor).handle(request)
    except MODEL_FAILURES as exc:
        run.error = f"executor: {type(exc).__name__}: {exc}"
        return run
    run.ms = (perf_counter() - started) * 1000
    run.reply = _only_result(replies, "executor")
    clock.observe(run.reply.lamport)
    run.parsed = json_object(llm.seen[-1]) is not None
    run.code = extract_code(run.reply.payload.get("result", ""))

    program = run.code + "\n\n" + "\n".join(task["tests"]) + "\n"
    result = await asyncio.to_thread(run_sandboxed, program, sandbox_timeout)
    run.exit_code, run.timed_out = result.exit_code, result.timed_out
    return run


def _row(pair: Pair, run: Execution, *, mock: bool) -> dict[str, Any]:
    """Every column on every row, so the CSV header never drifts."""
    row: dict[str, Any] = dict.fromkeys(ROW_COLUMNS, "")
    row.update(condition=pair.condition, task_id=str(run.task["id"]),
               conversation_id=run.conversation_id, executor_requested=pair.executor,
               reviewer_requested=pair.reviewer, mock=int(mock), error=run.error)
    if run.reply is None:
        return row
    code = run.code.encode("utf-8")
    row.update(
        executor_model=run.reply.cost.model if run.reply.cost else "",
        exit_code=run.exit_code, timed_out=int(run.timed_out),
        ground_truth="good" if run.exit_code == 0 else "bad",
        executor_status=str(run.reply.task_status),
        executor_claims_done=int(run.reply.task_status is TaskStatus.COMPLETE),
        executor_parsed=int(run.parsed),
        code_sha256="sha256:" + hashlib.sha256(code).hexdigest(), code_bytes=len(code),
        executor_ms=f"{run.ms:.3f}",
    )
    return row


async def review(
    run: Execution, *, pair: Pair, backend: Any, clock: LamportClock, mock: bool = False,
) -> dict[str, Any]:
    """Phase 2: a blind review, then the same reply replayed past the arbiter."""
    row = _row(pair, run, mock=mock)
    if run.reply is None:
        return row
    request = _request(
        clock=clock, receiver=REVIEWER, conversation_id=run.conversation_id,
        root_task=str(run.task["task"]), turn=run.reply.turn + 1, reply_to=run.reply.message_id,
        payload={"subtask": REVIEW_SUBTASK, "task": str(run.task["task"]), **_brief(run.task),
                 "code": run.code, "instructions": REVIEWER_INSTRUCTIONS},
    )
    llm = Normalising(backend)
    started = perf_counter()
    try:
        replies = await Agent(llm=llm, agent_path=REVIEWER, model=pair.reviewer).handle(request)
        ms = (perf_counter() - started) * 1000
        advisory = _only_result(replies, "reviewer")
        model = advisory.cost.model if advisory.cost else ""
        # Same request, same reply text; the runtime's exit code is the only change.
        replay = Agent(llm=MockBackend([llm.seen[-1]], model=model, simulate_cache=False),
                       agent_path=REVIEWER, model=pair.reviewer)
        graded = request.model_copy(
            update={"payload": {**request.payload, "exit_code": run.exit_code}})
        arbitrated = _only_result(await replay.handle(graded), "reviewer replay")
    except MODEL_FAILURES as exc:
        row["error"] = f"reviewer: {type(exc).__name__}: {exc}"
        return row
    clock.observe(advisory.lamport)

    good = run.exit_code == 0
    accept = advisory.task_status is TaskStatus.COMPLETE
    arbitrated_accept = arbitrated.task_status is TaskStatus.COMPLETE
    parsed = json_object(llm.seen[-1]) or {}
    row.update(
        reviewer_model=model, review_status=str(advisory.task_status),
        review_accept=int(accept), review_parsed=int(bool(parsed)),
        # In P2's _arbitrate a model-reported exit_code outranks the runtime's.
        review_claimed_exit_code=str(parsed["exit_code"]) if "exit_code" in parsed else "",
        arbitrated_status=str(arbitrated.task_status), arbitrated_accept=int(arbitrated_accept),
        disagree=int(row["executor_claims_done"] != int(accept)),
        outcome=OUTCOMES[(good, accept)], arbitrated_outcome=OUTCOMES[(good, arbitrated_accept)],
        review_ms=f"{ms:.3f}",
    )
    return row


def _rate(numerator: int, denominator: int) -> float | str:
    return numerator / denominator if denominator else ""


def summarize_pairs(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per condition: the confusion matrix, its rates, review latency."""
    for row in rows:
        if row["condition"] not in CONDITIONS:
            raise ValueError(f"unknown pair: {row['condition']!r}; expected one of {CONDITIONS}")
    summary: list[dict[str, Any]] = []
    for condition in CONDITIONS:
        group = [r for r in rows if r["condition"] == condition]
        if not group:
            continue
        scored = [r for r in group if not r["error"]]
        cells = {name: sum(r["outcome"] == name for r in scored) for name in OUTCOMES.values()}
        arbitrated = {name: sum(r["arbitrated_outcome"] == name for r in scored)
                      for name in OUTCOMES.values()}
        n_good = cells["true_accept"] + cells["false_reject"]
        n_bad = cells["false_accept"] + cells["catch"]
        review_ms = [float(r["review_ms"]) for r in scored]
        summary.append({
            "condition": condition, "executor": group[0]["executor_requested"],
            "reviewer": group[0]["reviewer_requested"],
            "mock": max(int(r["mock"]) for r in group),
            "n_tasks": len(scored), "n_errors": len(group) - len(scored),
            "n_good": n_good, "n_bad": n_bad, **cells,
            "n_review_unparsed": sum(int(r["review_parsed"]) == 0 for r in scored),
            "disagreement_rate": _rate(sum(int(r["disagree"]) for r in scored), len(scored)),
            "catch_rate": _rate(cells["catch"], n_bad),
            "false_accept_rate": _rate(cells["false_accept"], n_bad),
            "false_reject_rate": _rate(cells["false_reject"], n_good),
            "arbitrated_false_accept_rate": _rate(arbitrated["false_accept"], n_bad),
            "arbitrated_false_reject_rate": _rate(arbitrated["false_reject"], n_good),
            "review_ms_median": harness.median(review_ms) if review_ms else "",
            "review_ms_p95": harness.p95(review_ms) if review_ms else "",
        })
    return summary


def _label(backend: Any, requested: str) -> str:
    """What actually answered: the backend's model id, or `mock[...]` under the mock."""
    if isinstance(backend, MockBackend):
        return f"mock[{requested}]"
    return str(getattr(backend, "model_id", requested))


async def run(
    *, pairs: Sequence[str] = CONDITIONS, models: Mapping[str, tuple[str, str]] | None = None,
    tasks_path: str | None = None, min_tasks: int = harness.DEFAULT_MIN_TASKS,
    network: str = DEFAULT_NETWORK, sandbox_timeout: float = SANDBOX_TIMEOUT_S,
    backend_factory: BackendFactory | None = None, write: bool = True,
    results_dir: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute each task once per executor, review it once per pair -> `(rows, summary)`.

    The default factory is `default_backend`, so MOCK_LLM=1 wins over any model
    named on the command line.
    """
    plan = make_pairs(pairs, models)
    tasks = load_tasks(tasks_path)
    if len(tasks) < min_tasks:
        raise ValueError(
            f"{len(tasks)} tasks per condition is below the floor of {min_tasks} "
            f"(docs/EXPERIMENTS.md: 'Minimum 20 tasks per condition. Below that you are "
            f"reporting noise.'). Add tasks, or lower --min-tasks deliberately.")

    factory = backend_factory or (lambda model, role: default_backend(model))
    clock = LamportClock()
    opened: list[Any] = []
    executors: dict[str, Any] = {}
    executions: dict[str, list[Execution]] = {}
    labels: dict[str, list[str]] = {EXECUTOR: [], REVIEWER: []}
    rows: list[dict[str, Any]] = []
    try:
        # Every execution first, then each pair's reviews: one model resident at a
        # time, which is what a 6 GB card running local Ollama can hold.
        for index, model in enumerate(dict.fromkeys(p.executor for p in plan)):
            executors[model] = factory(model, "executor")
            opened.append(executors[model])
            executions[model] = [
                await execute(task, backend=executors[model], executor=model, index=index,
                              clock=clock, sandbox_timeout=sandbox_timeout)
                for task in tasks
            ]
            if all(e.error for e in executions[model]):
                raise RuntimeError(f"every executor call to {model} failed, first: "
                                   f"{executions[model][0].error}")
        for pair in plan:
            backend = factory(pair.reviewer, "reviewer")
            opened.append(backend)
            executor_backend = executors[pair.executor]
            labels[EXECUTOR].append(f"{pair.condition}:{_label(executor_backend, pair.executor)}")
            labels[REVIEWER].append(f"{pair.condition}:{_label(backend, pair.reviewer)}")
            mock = isinstance(backend, MockBackend) or isinstance(executor_backend, MockBackend)
            for execution in executions[pair.executor]:
                rows.append(await review(execution, pair=pair, backend=backend,
                                         clock=clock, mock=mock))
    finally:
        for backend in opened:
            if hasattr(backend, "aclose"):
                await backend.aclose()

    summary = summarize_pairs(rows)
    if write:
        meta = harness.capture_meta(
            peers=PEERS, seed=0, network=network, source=tasks_path or str(TASKS_JSON),
            models={REQUESTER: NO_MODEL, **{p: ",".join(v) for p, v in labels.items()}},
        )
        out = harness.RESULTS_DIR if results_dir is None else results_dir
        harness.write_csv("disagreement_tasks", rows, meta=meta, results_dir=out)
        harness.write_csv("disagreement_summary", summary, meta=meta, results_dir=out)
    return rows, summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = harness.arg_parser("disagreement", "Measurement 5: correlated error")
    parser.add_argument("--pairs", default=",".join(CONDITIONS),
                        help=f"comma-separated subset of {','.join(CONDITIONS)}")
    for condition in CONDITIONS:
        parser.add_argument(f"--{condition}", default=None, metavar="EXECUTOR,REVIEWER",
                            help=f"default {','.join(DEFAULT_MODELS[condition])}")
    parser.add_argument("--sandbox-timeout", type=float, default=SANDBOX_TIMEOUT_S,
                        help="seconds the held-out tests may run per task")
    args = parser.parse_args(argv)

    models: dict[str, tuple[str, str]] = {}
    for condition in CONDITIONS:
        if (spec := getattr(args, condition)) is None:
            continue
        parts = [part.strip() for part in spec.split(",")]
        if len(parts) != 2 or not all(parts):
            parser.error(f"--{condition} takes EXECUTOR,REVIEWER, got {spec!r}")
        models[condition] = (parts[0], parts[1])

    pairs = tuple(p.strip() for p in args.pairs.split(",") if p.strip())
    network = args.network if args.network != harness.DEFAULT_NETWORK else DEFAULT_NETWORK
    try:
        _, summary = asyncio.run(run(
            pairs=pairs, models=models, tasks_path=args.tasks, min_tasks=args.min_tasks,
            network=network, sandbox_timeout=args.sandbox_timeout, results_dir=args.results_dir,
        ))
    except (ValueError, PaidBackendRefused) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    where = args.results_dir or harness.RESULTS_DIR
    lines = [f"-> {where}/disagreement_tasks.csv, {where}/disagreement_summary.csv", ""]
    if any(row["mock"] for row in summary):
        lines += ["MOCK: canned replies, no model ran. A plumbing check, not a result.", ""]
    line = "{:<7} {:>3} {:>4} {:>4} {:>9} {:>7} {:>10} {:>10} {:>10} {:>9}".format
    header = line("pair", "n", "err", "bad", "disagree", "catch", "false_acc", "false_rej",
                  "arb_f_acc", "unparsed")
    lines += [header, "-" * len(header)]
    lines += [line(r["condition"], r["n_tasks"], r["n_errors"], r["n_bad"],
                   *("-" if r[k] == "" else f"{r[k]:.1%}" for k in TABLE_RATES),
                   r["n_review_unparsed"]) for r in summary]
    lines += ["", ("false_acc: bad code the blind reviewer accepted, over bad code. "
                   "arb_f_acc: the same after the runtime arbiter saw the exit code.")]
    print("\n".join(lines), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
