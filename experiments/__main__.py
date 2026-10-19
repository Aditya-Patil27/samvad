# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""All five measurements, then every figure, from one command.

    python -m experiments                      # results/ -> docs/figures, docs/RESULTS.md
    python -m experiments --seed 7 --results-dir /tmp/run --no-plots
    python -m experiments --only topology,grounding

This adds no measurement logic. It calls each measurement's own `main()` in
order with the shared switches, so a run from here and a run by hand write
the same CSVs. Each still runs alone (`python -m experiments.transport`, ...),
which is how a condition this cannot reach gets run:

- transport `lan` needs four laptops and `--peers config/peers.yaml`, so this
  runs inproc and loopback only.

Transport and topology make two passes over the 30-task set: 60 tasks per
condition, the sample the reported runs used. Their own default of 20 passes
dates from a 3-task set and would run 600.

A measurement that fails does not stop the rest; the exit code is non-zero
and the failure is named, and its figure is reported as not run.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Callable, Sequence

from experiments import context_dedup, disagreement, grounding, harness, plots, topology, transport

#: (name, main, extra argv). Order is the doc's numbering.
STEPS: tuple[tuple[str, Callable[[Sequence[str] | None], int], tuple[str, ...]], ...] = (
    ("transport", transport.main, ("--conditions", "inproc,loopback", "--repeat", "2")),
    ("topology", topology.main, ("--transport", "inproc,loopback", "--repeat", "2")),
    ("grounding", grounding.main, ()),
    ("context_dedup", context_dedup.main, ()),
    ("disagreement", disagreement.main, ()),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments", description=__doc__.split("\n")[0])
    parser.add_argument("--seed", type=int, default=harness.DEFAULT_SEED)
    parser.add_argument("--results-dir", default=None,
                        help=f"where the CSVs go (default {harness.RESULTS_DIR})")
    parser.add_argument("--only", default=None,
                        help=f"comma-separated subset of {','.join(s[0] for s in STEPS)}")
    parser.add_argument("--no-plots", action="store_true",
                        help="write the CSVs, leave docs/ alone")
    parser.add_argument("--docs-dir", default=None,
                        help=f"where the figures and RESULTS.md go (default {plots.DOCS_DIR})")
    args = parser.parse_args(argv)

    wanted = {s.strip() for s in args.only.split(",")} if args.only else {s[0] for s in STEPS}
    unknown = wanted - {s[0] for s in STEPS}
    if unknown:
        parser.error(f"unknown measurement(s): {', '.join(sorted(unknown))}")

    shared = ["--seed", str(args.seed)]
    if args.results_dir:
        shared += ["--results-dir", args.results_dir]

    failed: list[str] = []
    for name, run, extra in STEPS:
        if name not in wanted:
            continue
        print(f"\n== {name}", file=sys.stderr)
        try:
            code = run([*shared, *extra])
        except Exception:  # noqa: BLE001 -- one broken measurement must not hide the other four
            traceback.print_exc()
            code = 1
        if code:
            failed.append(name)

    if not args.no_plots:
        print("\n== plots", file=sys.stderr)
        where = ["--results-dir", args.results_dir] if args.results_dir else []
        plots.main([*where, *(["--docs-dir", args.docs_dir] if args.docs_dir else [])])
    if failed:
        print(f"\nfailed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
