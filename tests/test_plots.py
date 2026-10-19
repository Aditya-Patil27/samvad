"""experiments/plots.py and experiments/svg.py: figures redrawn from CSV only.

The CSVs here are written by the measurements' own `main()`, into tmp_path,
so a renamed summary column breaks this suite rather than the report.
"""
from __future__ import annotations

import csv
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from experiments import context_dedup, grounding, plots, svg, topology, transport

SEED = 20260904
PROSE_ABOVE = "# Results\n\nHand-written, above the block.\n\n"
PROSE_BELOW = "\n\n## Expected, got, surprised\n\nHand-written, below it.\n"


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("results")
    shared = ["--seed", str(SEED), "--results-dir", str(out)]
    assert transport.main([*shared, "--conditions", "inproc", "--repeat", "7"]) == 0
    assert topology.main(["--results-dir", str(out), "--repeat", "7"]) == 0
    assert grounding.main([*shared, "--min-tasks", "20"]) == 0
    assert context_dedup.main([*shared, "--min-tasks", "20"]) == 0
    return out


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    (tmp_path / "RESULTS.md").write_text(
        PROSE_ABOVE + plots.BEGIN + "\nstale\n" + plots.END + PROSE_BELOW, encoding="utf-8")
    return tmp_path


def summary(results: Path, name: str) -> list[dict[str, str]]:
    with (results / f"{name}_summary.csv").open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --- render ------------------------------------------------------------------

def test_every_figure_with_a_csv_is_drawn_and_parses(results: Path, docs: Path) -> None:
    drawn, missing = plots.render(results, docs)
    have = {f.name for f in plots.FIGURES if (results / f"{f.csv}.csv").exists()}
    assert set(drawn) == have and set(missing) == {f.name for f in plots.FIGURES} - have
    for name in drawn:
        root = ET.parse(docs / "figures" / f"{name}.svg").getroot()
        assert root.tag.endswith("svg")
        assert root.find("{http://www.w3.org/2000/svg}title") is not None


def test_the_prose_around_the_block_survives(results: Path, docs: Path) -> None:
    plots.render(results, docs)
    text = (docs / "RESULTS.md").read_text(encoding="utf-8")
    assert text.startswith(PROSE_ABOVE + plots.BEGIN)
    assert text.endswith(plots.END + PROSE_BELOW)
    assert "stale" not in text


def test_rendering_twice_is_a_no_op(results: Path, docs: Path) -> None:
    plots.render(results, docs)
    once = (docs / "RESULTS.md").read_text(encoding="utf-8")
    plots.render(results, docs)
    assert (docs / "RESULTS.md").read_text(encoding="utf-8") == once


def test_a_missing_csv_is_reported_with_its_command(docs: Path, tmp_path: Path) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()
    drawn, missing = plots.render(empty, docs, results_label="results")
    assert drawn == [] and missing == [f.name for f in plots.FIGURES]
    text = (docs / "RESULTS.md").read_text(encoding="utf-8")
    for fig in plots.FIGURES:
        assert f"Not run. `{fig.command}` writes `results/{fig.csv}.csv`." in text


def test_the_docs_name_the_csv_as_given_never_resolved(results: Path, docs: Path) -> None:
    plots.render(results, docs, results_label="results")
    text = (docs / "RESULTS.md").read_text(encoding="utf-8")
    assert str(results) not in text and str(results.parent) not in text
    for name in plots.render(results, docs)[0]:
        assert str(results) not in (docs / "figures" / f"{name}.svg").read_text(encoding="utf-8")


def test_provenance_comes_from_the_csv(results: Path, docs: Path) -> None:
    plots.render(results, docs)
    text = (docs / "RESULTS.md").read_text(encoding="utf-8")
    row = summary(results, "grounding")[0]
    assert f"sha `{row['run_git_sha']}` · {row['run_date']}" in text
    assert f"seed {row['run_seed']}" in text


def test_table_numbers_are_the_csv_numbers(results: Path, docs: Path) -> None:
    plots.render(results, docs)
    text = (docs / "RESULTS.md").read_text(encoding="utf-8")
    by = {r["condition"]: r for r in summary(results, "context_dedup")}
    inline, refs = by["inline"]["input_tokens_median"], by["refs"]["input_tokens_median"]
    assert f"| input tokens | {plots._num(inline)} | {plots._num(refs)} |" in text


def test_topology_is_three_lines_of_measured_counts_and_says_it_is_not_a_fit(
        results: Path, docs: Path) -> None:
    plots.render(results, docs)
    text = (docs / "RESULTS.md").read_text(encoding="utf-8")
    fig = (docs / "figures" / "topology.svg").read_text(encoding="utf-8")
    assert "not a fit" in fig
    for r in summary(results, "topology"):
        assert (f"| {r['transport']} | {r['shape']} | {r['n_nodes']} | "
                f"{r['n_completed']}/{r['n_tasks']} | {plots._num(r['messages_median'])} | "
                f"{r['theory_messages']} |") in text
    root = ET.fromstring(fig)
    assert len(root.findall(".//{http://www.w3.org/2000/svg}circle")) == 9  # 3 shapes x 3 N


def test_a_sweep_over_n_names_the_range_of_nodes(results: Path, docs: Path) -> None:
    plots.render(results, docs)
    text = (docs / "RESULTS.md").read_text(encoding="utf-8")
    assert "`results/topology_summary.csv`" in text
    assert "· 2–4 nodes ·" in text


def test_a_block_without_markers_is_refused(results: Path, tmp_path: Path) -> None:
    (tmp_path / "RESULTS.md").write_text("# Results\n", encoding="utf-8")
    with pytest.raises(ValueError, match="figures:begin"):
        plots.render(results, tmp_path)


def test_svgs_are_utf8_with_lf(results: Path, docs: Path) -> None:
    for name in plots.render(results, docs)[0]:
        raw = (docs / "figures" / f"{name}.svg").read_bytes()
        assert b"\r\n" not in raw
        raw.decode("utf-8")


# --- svg ---------------------------------------------------------------------

def test_nice_max_rounds_up_to_one_two_or_five() -> None:
    assert [svg._nice_max(v) for v in (0.83, 1.0, 37, 1200, 0)] == [1.0, 1.0, 50, 2000, 1.0]


def test_more_than_three_series_is_a_facet_not_a_fourth_hue() -> None:
    four = [svg.Series(str(i), [1.0]) for i in range(4)]
    with pytest.raises(ValueError, match="facet"):
        svg.bar_chart(title="t", subtitle="s", caption="c", categories=["a"], series=four)


def test_a_none_value_is_not_drawn() -> None:
    out = svg.bar_chart(title="t", subtitle="s", caption="c", categories=["a", "b"],
                        series=[svg.Series("x", [0.5, None])])
    assert out.count('class="s1 bar"') == 1


def test_every_mark_carries_a_title() -> None:
    out = svg.line_chart(title="t", subtitle="s", caption="c", x=[2, 3, 4], x_label="N",
                         series=[svg.Series("star", [2, 3, 4]), svg.Series("mesh", [2, 6, 12])])
    root = ET.fromstring(out)
    marks = root.findall(".//{http://www.w3.org/2000/svg}circle")
    assert len(marks) == 6
    assert all(m.find("{http://www.w3.org/2000/svg}title") is not None for m in marks)


def test_series_that_meet_are_named_where_they_meet() -> None:
    out = svg.line_chart(title="t", subtitle="s", caption="c", x=[2, 3, 4], x_label="N",
                         series=[svg.Series("star", [2, 6, 9]), svg.Series("ring", [2, 4, 6]),
                                 svg.Series("mesh", [2, 6, 12])])
    root = ET.fromstring(out)
    ns = "{http://www.w3.org/2000/svg}"
    labels = [t.text for t in root.findall(f"{ns}text")]
    assert "star = ring = mesh" in labels and "star = mesh" in labels
    titles = [c.find(f"{ns}title").text for c in root.findall(f".//{ns}circle")]
    assert titles.count("star = mesh, N 3: 6") == 2
    assert "ring, N 3: 4" in titles


def test_text_is_escaped() -> None:
    out = svg.bar_chart(title="a < b & c", subtitle="s", caption="c",
                        categories=["<x>"], series=[svg.Series("y", [1.0])])
    ET.fromstring(out)
