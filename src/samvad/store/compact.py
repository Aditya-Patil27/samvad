# OWNER: P3 -- see docs/WORK.md. Do not edit if you are not P3.
"""Hot / warm / cold tiering.

    hot    last N turns, verbatim
    warm   older turns, summarised
    cold   SQLite, retrieved on demand

Each agent compacts independently -- no coordination needed.

root_task NEVER compacts. It is immutable for the whole conversation and is
re-injected verbatim into every prompt. By turn six, agents that only see
paraphrases are solving a different problem.
"""


class Compactor:
    def compact(self, history: list) -> list:
        raise NotImplementedError
