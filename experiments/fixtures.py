# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Synthetic message log generator.

BUILD THIS FIRST, in week 1. P4 has nothing real to observe until week 2, so
the dashboard and every experiment get built against fake data and swapped
onto the real log later. Do not sit and wait.
"""


def synthetic_log(n_messages: int = 200, n_agents: int = 4) -> list:
    """Plausible message log: correct Lamport ordering, realistic costs,
    a mix of evidence types, some spawn trees."""
    raise NotImplementedError
