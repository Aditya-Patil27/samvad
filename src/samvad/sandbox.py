# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""Run model-generated code without trusting it.

Executor agents run whatever the model wrote. Rules, all mandatory:
    - tempdir cwd, cleaned afterwards
    - hard timeout, process killed on expiry
    - NEVER shell=True
    - no network access
"""
from dataclasses import dataclass


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


def run_sandboxed(code: str, timeout: float = 5.0) -> ExecResult:
    raise NotImplementedError
