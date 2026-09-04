"""Contract for the shared experiment harness (experiments/harness.py).

Written before the implementation, per CLAUDE.md. These do NOT skip: the
harness depends only on the standard library and experiments/fixtures.py, so
there is no stub to wait on and no reason to defer.

What these tests defend:

1. **Reproducibility.** docs/EXPERIMENTS.md: "Every plot in the report
   regenerates from CSV. No hand-typed numbers, ever." A CSV writer that
   reorders columns between runs makes a diff useless and a plot unverifiable.
2. **Run metadata.** Every run records git SHA, date, network, model per peer,
   node count. A CSV without those cannot be attributed to a build.
3. **median and p95, never a bare mean.** The doc is explicit: LLM latency is
   heavily skewed and a mean hides it. Both helpers live here so all five
   measurements report the same statistic computed the same way.
"""
import csv

import pytest

from experiments import harness

# --------------------------------------------------------------------------
# statistics -- median and p95, the two the report is required to carry
# --------------------------------------------------------------------------

def test_median_of_an_odd_sample_is_the_middle_value():
    assert harness.median([3, 1, 2]) == 2


def test_median_of_an_even_sample_is_the_midpoint():
    assert harness.median([1, 2, 3, 4]) == 2.5


def test_median_of_one_value_is_that_value():
    assert harness.median([7.5]) == 7.5


def test_p95_interpolates_between_ranks():
    """Linear interpolation on the sorted sample, the numpy default. Pinned so
    a later reimplementation cannot silently change every reported p95."""
    assert harness.p95(list(range(1, 101))) == pytest.approx(95.05)


def test_p95_of_one_value_is_that_value():
    assert harness.p95([4.0]) == 4.0


def test_p95_is_at_least_the_median_and_at_most_the_max():
    values = [1, 1, 2, 3, 5, 8, 13, 21, 34, 900]
    assert harness.median(values) <= harness.p95(values) <= max(values)


def test_p95_ignores_input_order():
    forward = harness.p95([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert forward == harness.p95([10, 9, 8, 7, 6, 5, 4, 3, 2, 1])


@pytest.mark.parametrize("fn", [harness.median, harness.p95])
def test_an_empty_sample_raises_rather_than_returning_zero(fn):
    """A zero here would land in a CSV and be plotted as a real measurement."""
    with pytest.raises(ValueError):
        fn([])


def test_summarize_reports_median_and_p95_together():
    """A mean alone is what the doc forbids; summarize never emits one alone."""
    stats = harness.summarize([1, 2, 3, 4, 5])
    assert stats["n"] == 5
    assert stats["median"] == 3
    assert stats["p95"] == pytest.approx(4.8)
    assert stats["mean"] == 3
    assert stats["min"] == 1
    assert stats["max"] == 5


def test_summarize_prefixes_every_key_so_two_series_share_one_row():
    stats = harness.summarize([1, 2, 3], prefix="latency_ms")
    assert set(stats) == {
        "latency_ms_n", "latency_ms_median", "latency_ms_p95",
        "latency_ms_mean", "latency_ms_min", "latency_ms_max",
    }


# --------------------------------------------------------------------------
# run metadata
# --------------------------------------------------------------------------

def test_capture_meta_records_everything_the_doc_requires():
    meta = harness.capture_meta(
        peers=["agent_a", "agent_b", "agent_c"], seed=11,
        network="ethernet", date="2026-09-04",
    )
    assert meta.node_count == 3
    assert meta.network == "ethernet"
    assert meta.date == "2026-09-04"
    assert meta.seed == 11
    assert "agent_a=" in meta.models and "agent_c=" in meta.models


def test_capture_meta_takes_the_model_per_peer_from_the_fixture_peer_table():
    from experiments import fixtures
    meta = harness.capture_meta(peers=["agent_c"], seed=0, date="2026-09-04")
    assert meta.models == f"agent_c={fixtures.model_for('agent_c')}"


def test_capture_meta_defaults_the_date_to_today_not_to_a_placeholder():
    meta = harness.capture_meta(peers=["agent_a"], seed=0)
    assert len(meta.date) == 10 and meta.date.count("-") == 2


def test_git_sha_is_a_short_hex_sha_or_the_explicit_word_unknown():
    sha = harness.git_sha()
    assert sha == "unknown" or (7 <= len(sha) <= 40 and all(c in "0123456789abcdef" for c in sha))


def test_meta_columns_are_prefixed_so_they_cannot_collide_with_measurements():
    meta = harness.capture_meta(peers=["agent_a"], seed=0, date="2026-09-04")
    columns = meta.as_columns()
    assert all(k.startswith("run_") for k in columns)
    assert columns["run_git_sha"] == meta.git_sha
    assert columns["run_node_count"] == "1"


# --------------------------------------------------------------------------
# CSV writing
# --------------------------------------------------------------------------

def test_write_csv_creates_the_results_dir_and_names_the_file(tmp_path):
    out = harness.write_csv("demo", [{"a": 1}], results_dir=tmp_path / "results")
    assert out.name == "demo.csv"
    assert out.exists()


def test_write_csv_puts_run_metadata_on_every_row(tmp_path):
    meta = harness.capture_meta(peers=["agent_a"], seed=0, date="2026-09-04")
    out = harness.write_csv(
        "demo", [{"a": 1}, {"a": 2}], meta=meta, results_dir=tmp_path)
    rows = list(csv.DictReader(out.open(newline="", encoding="utf-8")))
    assert len(rows) == 2
    for row in rows:
        assert row["run_git_sha"] == meta.git_sha
        assert row["run_date"] == "2026-09-04"
        assert row["run_network"] == meta.network
        assert row["run_node_count"] == "1"
    assert [row["a"] for row in rows] == ["1", "2"]


def test_write_csv_is_byte_identical_for_identical_input(tmp_path):
    """Same rows, same meta, same bytes -- otherwise 'deterministic' is a claim
    nobody can check with a diff."""
    meta = harness.capture_meta(peers=["agent_a"], seed=0, date="2026-09-04")
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    first = harness.write_csv("one", rows, meta=meta, results_dir=tmp_path).read_bytes()
    second = harness.write_csv("two", rows, meta=meta, results_dir=tmp_path).read_bytes()
    assert first == second


def test_write_csv_keeps_column_order_stable_across_rows(tmp_path):
    out = harness.write_csv(
        "demo", [{"a": 1, "b": 2}, {"b": 3, "a": 4}], results_dir=tmp_path)
    header = out.read_text(encoding="utf-8").splitlines()[0]
    assert header == "a,b"


def test_write_csv_refuses_a_row_carrying_an_unannounced_column(tmp_path):
    """A silently dropped column is a measurement that vanishes from the plot."""
    with pytest.raises(ValueError):
        harness.write_csv("demo", [{"a": 1}, {"a": 2, "surprise": 3}], results_dir=tmp_path)


def test_write_csv_refuses_an_empty_result_set(tmp_path):
    with pytest.raises(ValueError):
        harness.write_csv("demo", [], results_dir=tmp_path)


def test_write_csv_defaults_into_the_repo_results_dir():
    assert harness.RESULTS_DIR.name == "results"
    assert harness.RESULTS_DIR.parent.name == "cn_cp" or harness.RESULTS_DIR.parent.is_dir()


# --------------------------------------------------------------------------
# shared CLI surface
# --------------------------------------------------------------------------

def test_common_args_give_every_measurement_the_same_switches():
    parser = harness.arg_parser("demo")
    args = parser.parse_args([])
    assert args.seed == harness.DEFAULT_SEED
    assert args.min_tasks == 20      # docs/EXPERIMENTS.md: below 20 you report noise
    assert args.network == harness.DEFAULT_NETWORK
    assert args.results_dir is None
    assert args.messages is None


def test_common_args_parse_the_documented_tasks_switch():
    """docs/EXPERIMENTS.md invokes every measurement with --tasks."""
    args = harness.arg_parser("demo").parse_args(["--tasks", "experiments/tasks.json"])
    assert args.tasks.endswith("tasks.json")


# --------------------------------------------------------------------------
# log slicing -- shared by the measurements that count per task
# --------------------------------------------------------------------------

def test_whole_conversations_drops_a_conversation_the_log_cut_in_half():
    msgs = [
        {"conversation_id": "conv-1", "lamport": 1},
        {"conversation_id": "conv-1", "lamport": 2},
        {"conversation_id": "conv-2", "lamport": 3},
    ]
    kept = harness.whole_conversations(msgs)
    assert {m["conversation_id"] for m in kept} == {"conv-1"}


def test_whole_conversations_keeps_a_single_conversation_rather_than_returning_nothing():
    msgs = [{"conversation_id": "conv-1", "lamport": 1}]
    assert harness.whole_conversations(msgs) == msgs


def test_log_with_at_least_grows_the_log_until_it_holds_enough_tasks():
    frames, n_messages = harness.log_with_at_least(min_tasks=22, seed=3)
    from experiments import fixtures
    convs = {m["conversation_id"] for m in fixtures.messages(frames)}
    assert len(convs) >= 22
    assert n_messages >= 22


def test_log_with_at_least_is_deterministic_for_a_seed():
    assert harness.log_with_at_least(min_tasks=21, seed=5) == \
        harness.log_with_at_least(min_tasks=21, seed=5)
