# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Measurement 2: star vs ring vs mesh -- O(N) against O(N^2)

Method: docs/EXPERIMENTS.md

Writes CSV to results/. Every plot in the report regenerates from that CSV --
no hand-typed numbers. Record git SHA, date, network, model per peer, node
count. Report median AND p95: LLM latency is heavily skewed and a mean hides
it. Minimum 20 tasks per condition.
"""


def run() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    run()
