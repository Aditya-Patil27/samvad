# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Shared experiment harness: CSV out, run metadata in, median and p95.

All five measurements in docs/EXPERIMENTS.md need exactly the same three
things, and the doc mandates each of them:

* **A CSV per run, in `results/`.** "Every plot in the report regenerates from
  CSV. No hand-typed numbers, ever." So the writer is here, once, and every
  measurement gets stable column order and byte-identical output for identical
  input -- otherwise `git diff` between two runs is unreadable and nobody can
  check the claim of reproducibility.
* **Run metadata on every row.** "Every run records: git SHA, date, network,
  model per peer, node count." Carried as `run_*` columns so a CSV can be
  concatenated with another run's and still be attributable. A sidecar file
  would get separated from its data within a week.
* **`median` and `p95`.** "Report median AND p95, not just the mean. LLM
  latency is heavily skewed and a mean hides it." Both live here so all five
  measurements compute the same statistic the same way; `summarize()` emits
  them together with the mean, never a mean alone.

**This module deliberately does not import `samvad.protocol`.** Same reason
`experiments/fixtures.py` does not: that module is a frozen week-1 joint task
and a harness that waits for it arrives after the report is due. The harness
reads plain frame dicts -- fixture output today, `/events` frames later.

Measurements 1, 2 and 5 are expected to build on this file unchanged.
"""
from __future__ import annotations

import argparse
import csv
import math
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments import fixtures

#: Repo-root `results/`. Gitignored except `.gitkeep` -- write here, commit nothing.
RESULTS_DIR: Path = Path(__file__).resolve().parent.parent / "results"

#: What the network column says when no packet crossed one. Measurements 3 and 4
#: run against the synthetic log, so claiming `campus-wifi` would be a lie in a
#: column whose whole job is to say where the numbers came from.
DEFAULT_NETWORK = "none-synthetic"

#: docs/EXPERIMENTS.md: "Minimum 20 tasks per condition. Below that you are
#: reporting noise."
DEFAULT_MIN_TASKS = 20

DEFAULT_SEED = 20260904


# --- statistics ------------------------------------------------------------

def median(values: Sequence[float] | Iterable[float]) -> float:
    """Middle of the sample; the midpoint of the two middles when even."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("median of an empty sample")
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def p95(values: Sequence[float] | Iterable[float]) -> float:
    """95th percentile by linear interpolation between ranks.

    The same definition numpy uses by default, pinned by a test, because a p95
    that changes meaning between two implementations changes every number in
    the report without changing a single measurement.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("p95 of an empty sample")
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * 0.95
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def summarize(values: Sequence[float] | Iterable[float], *, prefix: str = "") -> dict[str, float]:
    """`n / median / p95 / mean / min / max` for one series.

    The mean is included, never alone: docs/EXPERIMENTS.md forbids reporting it
    on its own, and the cheapest way to enforce that is to make it impossible
    to get the mean out of the harness without the median and p95 beside it.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("summarize of an empty sample")
    stats = {
        "n": len(ordered),
        "median": median(ordered),
        "p95": p95(ordered),
        "mean": sum(ordered) / len(ordered),
        "min": float(ordered[0]),
        "max": float(ordered[-1]),
    }
    if not prefix:
        return stats
    return {f"{prefix}_{k}": v for k, v in stats.items()}


# --- run metadata ----------------------------------------------------------

@dataclass(frozen=True)
class RunMeta:
    """The five things docs/EXPERIMENTS.md requires every run to record, plus
    the seed and data source, which are what make a synthetic run reproducible."""

    git_sha: str
    date: str
    network: str
    node_count: int
    models: str
    seed: int
    source: str

    def as_columns(self) -> dict[str, str]:
        """`run_`-prefixed so metadata can never collide with a measured column."""
        return {
            "run_git_sha": self.git_sha,
            "run_date": self.date,
            "run_network": self.network,
            "run_node_count": str(self.node_count),
            "run_models": self.models,
            "run_seed": str(self.seed),
            "run_source": self.source,
        }


def git_sha(repo: Path | None = None) -> str:
    """Short SHA of HEAD, or `"unknown"` outside a checkout.

    No `shell=True` -- CLAUDE.md, and this runs in CI. A failure returns the
    literal string rather than raising: a missing SHA should degrade the
    provenance of a CSV, not lose the measurement that produced it.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo or Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else "unknown"


def capture_meta(
    *, peers: Sequence[str], seed: int, network: str = DEFAULT_NETWORK,
    date: str | None = None, source: str = "experiments.fixtures.synthetic_log",
    models: Mapping[str, str] | None = None,
) -> RunMeta:
    """Collect the run's provenance. `date` is injectable so a test can pin it."""
    table = dict(models) if models else {p: fixtures.model_for(p) for p in peers}
    return RunMeta(
        git_sha=git_sha(),
        date=date or datetime.now(UTC).date().isoformat(),
        network=network,
        node_count=len(peers),
        models=";".join(f"{p}={table[p]}" for p in peers),
        seed=seed,
        source=source,
    )


# --- CSV -------------------------------------------------------------------

def write_csv(
    name: str, rows: Sequence[Mapping[str, Any]], *, meta: RunMeta | None = None,
    results_dir: Path | None = None,
) -> Path:
    """Write `rows` to `results/<name>.csv` and return the path.

    Column order is the first row's key order, metadata first. Rows are checked
    against that header rather than filled in: a key that appears only on a
    later row is a column the plot would silently lose, which is exactly the
    class of bug that turns into a wrong number in a report.
    """
    if not rows:
        raise ValueError(f"refusing to write an empty CSV: {name}")
    out_dir = Path(results_dir) if results_dir is not None else RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.csv"

    meta_columns = meta.as_columns() if meta else {}
    fieldnames = list(meta_columns) + list(rows[0])
    for i, row in enumerate(rows):
        unexpected = set(row) - set(fieldnames)
        if unexpected:
            raise ValueError(f"row {i} of {name} carries unannounced columns: {sorted(unexpected)}")
        missing = set(fieldnames) - set(meta_columns) - set(row)
        if missing:
            raise ValueError(f"row {i} of {name} is missing columns: {sorted(missing)}")

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({**meta_columns, **row})
    return path


# --- shared CLI ------------------------------------------------------------

def arg_parser(prog: str, description: str = "") -> argparse.ArgumentParser:
    """The switches every measurement shares, so all five are invoked alike."""
    parser = argparse.ArgumentParser(prog=f"experiments.{prog}", description=description)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help="fixture seed; same seed, same CSV")
    parser.add_argument("--min-tasks", type=int, default=DEFAULT_MIN_TASKS,
                        help="minimum complete tasks to measure (doc floor: 20)")
    parser.add_argument("--messages", type=int, default=None,
                        help="fixture log length; overrides --min-tasks sizing")
    parser.add_argument("--agents", type=int, default=len(fixtures.PEERS),
                        help="peer count in the synthetic run")
    parser.add_argument("--network", default=DEFAULT_NETWORK,
                        help="network label recorded in the CSV")
    parser.add_argument("--tasks", default=None,
                        help="task set recorded in the CSV; the synthetic log supplies "
                             "its own root tasks, so this labels a run, it does not drive it")
    parser.add_argument("--results-dir", default=None,
                        help=f"where the CSVs go (default {RESULTS_DIR})")
    return parser


# --- log slicing -----------------------------------------------------------

def whole_conversations(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Drop the last conversation, which the log's length cap may have cut.

    `fixtures.synthetic_log` truncates to exactly `n_messages`, mid-scene if it
    lands there -- the honest shape for a log sampled from a run still going.
    A half-conversation would still be counted as a whole task by anything
    measuring per task, so it is dropped rather than measured. A log holding
    only one conversation is returned as-is: there is nothing to compare it to
    either way, and returning nothing would be a silent empty measurement.
    """
    order: list[str] = []
    for m in messages:
        if m["conversation_id"] not in order:
            order.append(m["conversation_id"])
    if len(order) < 2:
        return list(messages)
    last = order[-1]
    return [m for m in messages if m["conversation_id"] != last]


def log_with_at_least(
    *, min_tasks: int, seed: int, n_agents: int = len(fixtures.PEERS),
    n_messages: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """A synthetic log holding at least `min_tasks` complete conversations.

    The generator is parameterised by message count, not task count, so this
    grows the log until enough whole tasks are in it. Deterministic: the growth
    schedule is fixed, so `(min_tasks, seed, n_agents)` always yields one log.
    """
    if min_tasks < 1:
        raise ValueError(f"min_tasks must be >= 1, got {min_tasks}")
    if n_messages is not None:
        return fixtures.synthetic_log(n_messages, n_agents, seed=seed), n_messages
    size = max(64, min_tasks * 16)
    for _ in range(12):
        frames = fixtures.synthetic_log(size, n_agents, seed=seed)
        kept = whole_conversations(fixtures.messages(frames))
        if len({m["conversation_id"] for m in kept}) >= min_tasks:
            return frames, size
        size = int(size * 1.5) + 32
    raise RuntimeError(f"could not reach {min_tasks} tasks within 12 growth steps")
