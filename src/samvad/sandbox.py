# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""Run model-generated code without trusting it.

Executor agents run whatever the model wrote. Rules, all mandatory:
    - tempdir cwd, cleaned afterwards
    - hard timeout, process killed on expiry
    - NEVER shell=True
    - no network access -- but see the limitation below; this one is partial

A failure is a non-zero `exit_code`, never a raised exception. That matters
beyond tidiness: `task_status: complete` is gated on `exit_code == 0` in a code
path, so the executor has to survive the model's worst code and still report a
number the runtime can arbitrate on.

WHAT THIS ACTUALLY GUARANTEES -- measured, not assumed
------------------------------------------------------
Verified by test: an infinite loop is killed at the timeout (exit 124, the
process reaped, not abandoned); syntax errors and crashes return non-zero
rather than raising; no shell is ever spawned; the child's cwd is a throwaway
directory that cannot see the repo; and the parent's environment -- API keys
above all -- is filtered to `_ENV_ALLOWLIST`, so a planted key is invisible to
the child.

THE NETWORK BLOCK IS PARTIAL. It monkey-patches the `socket` module and the
stdlib entry points above it, which stops urllib, httpx, requests and
http.client -- every route ordinary code takes. It does NOT stop
`import _socket`, which reaches the C extension directly and connects. This is
measured behaviour, not a suspicion.

So: this is defence against a model that wanders onto the network by accident,
not against code deliberately trying to escape. It is not an OS-level sandbox.
The child still runs as this user and can read the filesystem. Code from an
untrusted peer needs a container, a VM, or a firewall rule UNDERNEATH this --
not instead of it, and not this alone.

Closing the `_socket` hole in-process is not worth attempting: anything
reachable by patching is reachable by unpatching, and a guard that looks
airtight but is not is worse than one whose edge is written down.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Exit code reported when the child is killed at the timeout. 124 is the
#: convention GNU `timeout` uses; the real returncode after a kill differs per
#: platform, and callers should not have to care which one they are on.
TIMEOUT_EXIT_CODE = 124

#: Output past this is dropped. Validate at boundaries -- an executor that
#: forwards a 5 MB print() into a message would blow the receiver's context.
MAX_OUTPUT_BYTES = 64 * 1024

#: Environment the child is allowed to see. Everything else -- API keys above
#: all -- stays in the parent.
_ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")

_CODE_FILENAME = "main.py"
_RUNNER_FILENAME = "_samvad_runner.py"

#: Installed in the child before a single line of model code runs. An
#: in-process guard, not a firewall: it shuts the doors the stdlib actually
#: uses (constructing a socket, create_connection, DNS), which is every path
#: urllib, httpx, requests and http.client take.
#:
#: `socket.socket` stays a class and only its constructor is poisoned --
#: replacing the name with a function breaks `import ssl`, which subclasses
#: it at import time.
_RUNNER = '''\
"""Sandbox entry point. Blocks the network, then runs the model's code."""
import runpy
import socket
import sys

_DENIED = "samvad-sandbox: network access is disabled"


def _denied(*_args, **_kwargs):
    raise OSError(_DENIED)


socket.socket.__init__ = _denied

for _name in (
    "socketpair",
    "create_connection",
    "create_server",
    "fromfd",
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
):
    if hasattr(socket, _name):
        setattr(socket, _name, _denied)

sys.argv = ["main.py"]
runpy.run_path("main.py", run_name="__main__")
'''


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


def _child_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    # -I already ignores PYTHON* vars; this is belt and braces for a future
    # caller that drops the flag.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _clip(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    return text[:MAX_OUTPUT_BYTES] + "\n...[truncated by sandbox]"


def run_sandboxed(code: str, timeout: float = 5.0) -> ExecResult:
    """Execute `code` in a throwaway interpreter and report what happened.

    Never raises on account of the code it was given: a syntax error, a crash,
    an infinite loop and an unwritable temp directory all come back as an
    ExecResult with a non-zero exit code.
    """
    with tempfile.TemporaryDirectory(prefix="samvad-", ignore_cleanup_errors=True) as workdir:
        directory = Path(workdir)
        (directory / _CODE_FILENAME).write_text(code, encoding="utf-8")
        (directory / _RUNNER_FILENAME).write_text(_RUNNER, encoding="utf-8")

        # -I: isolated mode. No PYTHON* env vars, no user site-packages, and the
        # script's directory is kept off sys.path so a file the model names
        # `socket.py` cannot shadow the stdlib.
        argv = [sys.executable, "-I", "-X", "utf8", _RUNNER_FILENAME]

        try:
            # argv list, never a shell string -- CLAUDE.md: no shell=True, ever.
            completed = subprocess.run(
                argv,
                cwd=directory,
                env=_child_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as expired:
            # subprocess.run has already killed and reaped the child by the time
            # this is raised -- the process is gone, not merely abandoned.
            return ExecResult(
                exit_code=TIMEOUT_EXIT_CODE,
                stdout=_clip(expired.stdout),
                stderr=_clip(expired.stderr) + f"\nsamvad-sandbox: killed after {timeout}s",
                timed_out=True,
            )
        except OSError as exc:  # the interpreter itself could not be started
            return ExecResult(exit_code=1, stdout="", stderr=f"samvad-sandbox: {exc}", timed_out=False)

        return ExecResult(
            exit_code=completed.returncode,
            stdout=_clip(completed.stdout),
            stderr=_clip(completed.stderr),
            timed_out=False,
        )
