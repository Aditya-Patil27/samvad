"""python -m experiments: all five measurements, then the figures, from one command."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

import experiments.__main__ as run_all
from experiments import plots

SEED = "20260904"


def test_every_figure_has_a_step_and_every_step_a_figure() -> None:
    assert [name for name, _, _ in run_all.STEPS] == [f.name for f in plots.FIGURES]


def test_an_unknown_measurement_is_refused() -> None:
    with pytest.raises(SystemExit) as exc:
        run_all.main(["--only", "transport,nope"])
    assert exc.value.code == 2


def test_a_subset_writes_its_csvs_and_no_others(tmp_path: Path) -> None:
    code = run_all.main(["--only", "grounding,context_dedup", "--seed", SEED,
                         "--results-dir", str(tmp_path), "--no-plots"])
    assert code == 0
    written = {p.name for p in tmp_path.iterdir()}
    assert {"grounding_summary.csv", "context_dedup_summary.csv"} <= written
    assert not any(name.startswith(("transport", "topology", "disagreement")) for name in written)


def test_every_step_gets_the_shared_switches(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path) -> None:
    seen: dict[str, list[str]] = {}

    def record(name: str):
        def main(argv: Sequence[str] | None) -> int:
            seen[name] = list(argv or [])
            return 0
        return main

    monkeypatch.setattr(run_all, "STEPS", (("a", record("a"), ("--x", "1")),
                                           ("b", record("b"), ())))
    assert run_all.main(["--seed", "7", "--results-dir", str(tmp_path), "--no-plots"]) == 0
    assert seen["a"] == ["--seed", "7", "--results-dir", str(tmp_path), "--x", "1"]
    assert seen["b"] == ["--seed", "7", "--results-dir", str(tmp_path)]


def test_one_broken_measurement_does_not_hide_the_others(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    ran: list[str] = []

    def boom(argv: Sequence[str] | None) -> int:
        raise RuntimeError("measurement bug")

    def refused(argv: Sequence[str] | None) -> int:
        ran.append("refused")
        return 2

    def fine(argv: Sequence[str] | None) -> int:
        ran.append("fine")
        return 0

    monkeypatch.setattr(run_all, "STEPS", (("boom", boom, ()), ("refused", refused, ()),
                                           ("fine", fine, ())))
    assert run_all.main(["--no-plots"]) == 1
    assert ran == ["refused", "fine"]
    err = capsys.readouterr().err
    assert "RuntimeError: measurement bug" in err
    assert "failed: boom, refused" in err


def test_the_figures_go_where_they_are_told(tmp_path: Path) -> None:
    results, docs = tmp_path / "results", tmp_path / "docs"
    docs.mkdir()
    (docs / "RESULTS.md").write_text(f"# R\n\n{plots.BEGIN}\n{plots.END}\n", encoding="utf-8")
    assert run_all.main(["--only", "grounding", "--seed", SEED, "--results-dir", str(results),
                         "--docs-dir", str(docs)]) == 0
    assert (docs / "figures" / "grounding.svg").exists()
    text = (docs / "RESULTS.md").read_text(encoding="utf-8")
    assert "![3 — Grounded-claim ratio](figures/grounding.svg)" in text
    assert "### 1 — Transport comparison\n\nNot run." in text
