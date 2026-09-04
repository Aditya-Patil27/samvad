"""Contract: the sandbox. Seam between P2's executor and code the model wrote.

Deliberately NOT gated on protocol.py. run_sandboxed touches no envelope, so it
can be built -- and must be proven -- before the week-1 joint session lands.

The rules under test are transcribed from src/samvad/sandbox.py's docstring and
from CLAUDE.md: tempdir cwd cleaned afterwards, a hard timeout that actually
kills, never shell=True, no network, and -- the one that matters most for the
`task_status: complete requires exit_code == 0` control -- a failure is a
non-zero exit code, never a raised exception. An executor that crashes on bad
code cannot report the exit code the runtime is supposed to arbitrate on.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from samvad.sandbox import _ENV_ALLOWLIST, ExecResult, run_sandboxed

# --- the happy path ---------------------------------------------------------


def test_returns_an_exec_result_with_stdout_and_exit_zero():
    result = run_sandboxed("print('hello from the sandbox')")
    assert isinstance(result, ExecResult)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert "hello from the sandbox" in result.stdout


def test_stderr_is_captured_separately_from_stdout():
    result = run_sandboxed("import sys; sys.stderr.write('on stderr')")
    assert result.exit_code == 0
    assert "on stderr" in result.stderr
    assert "on stderr" not in result.stdout


def test_explicit_exit_code_is_propagated():
    """The runtime is the arbiter, so the number it reports has to be the real one."""
    assert run_sandboxed("raise SystemExit(3)").exit_code == 3


# --- failures are exit codes, never exceptions ------------------------------


def test_a_syntax_error_is_a_non_zero_exit_code_not_an_exception():
    result = run_sandboxed("def broken(:\n    pass")
    assert result.exit_code != 0
    assert result.timed_out is False
    assert "SyntaxError" in result.stderr


def test_a_crash_is_a_non_zero_exit_code_not_an_exception():
    result = run_sandboxed("raise ValueError('the model wrote this')")
    assert result.exit_code != 0
    assert "ValueError" in result.stderr
    assert "the model wrote this" in result.stderr


def test_a_traceback_points_at_the_models_own_line_numbers():
    """A reviewer agent reads this traceback; harness frames would mislead it."""
    result = run_sandboxed("x = 1\ny = 2\nraise RuntimeError('boom')\n")
    assert result.exit_code != 0
    assert "line 3" in result.stderr


# --- the timeout ------------------------------------------------------------


def test_an_infinite_loop_is_killed_at_the_timeout():
    started = time.monotonic()
    result = run_sandboxed("while True: pass", timeout=1.0)
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.exit_code != 0
    assert elapsed < 15.0, "the process was abandoned, not killed"


def test_a_blocking_read_does_not_hang_the_parent():
    """stdin must not be the parent's, or input() waits for the whole timeout."""
    result = run_sandboxed("input()", timeout=10.0)
    assert result.timed_out is False
    assert result.exit_code != 0
    assert "EOFError" in result.stderr


# --- isolation --------------------------------------------------------------


def test_runs_in_a_temp_directory_that_is_cleaned_up_afterwards():
    result = run_sandboxed("import os; print(os.getcwd())")
    cwd = result.stdout.strip()

    assert cwd, "the sandbox did not report a working directory"
    assert Path(cwd) != Path(os.getcwd()), "model code ran in the repo checkout"
    assert not Path(cwd).exists(), "the temp directory was left behind"


def test_files_written_by_the_model_do_not_survive_the_run():
    result = run_sandboxed(
        "import os\n"
        "open('artifact.txt', 'w').write('x')\n"
        "print(os.path.abspath('artifact.txt'))\n"
    )
    assert result.exit_code == 0
    assert not Path(result.stdout.strip()).exists()


def test_the_network_is_not_reachable_from_the_child():
    """Blocked in-process before the model's code runs, so nothing leaves the box."""
    result = run_sandboxed(
        "import socket\n"
        "try:\n"
        "    socket.socket()\n"
        "except OSError as exc:\n"
        "    print('socket:', exc)\n"
        "else:\n"
        "    print('socket: OPEN')\n"
    )
    assert result.exit_code == 0
    assert "network access is disabled" in result.stdout


def test_an_http_client_cannot_leave_the_box_either():
    """urllib goes through create_connection, not socket() -- both must be shut."""
    result = run_sandboxed(
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://127.0.0.1:1/')\n"
        "except Exception as exc:\n"
        "    print('urlopen:', exc)\n"
        "else:\n"
        "    print('urlopen: OPEN')\n"
    )
    assert result.exit_code == 0
    assert "network access is disabled" in result.stdout


def test_the_module_never_uses_a_shell():
    """CLAUDE.md: no shell=True, ever. Executors run model-generated code.

    A source assertion rather than a behavioural one on purpose: shell=True is a
    thing a later edit adds by accident, and by the time it shows up in
    behaviour the model's string is already being parsed by cmd.exe.
    """
    import ast

    import samvad.sandbox

    tree = ast.parse(Path(samvad.sandbox.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        assert all(kw.arg != "shell" for kw in node.keywords), "shell= passed to a call"
        called = ast.unparse(node.func)
        assert called not in {"os.system", "os.popen", "subprocess.getoutput"}, called


def test_model_output_cannot_flood_the_parent():
    """Validate at boundaries -- subprocess output is one."""
    result = run_sandboxed("print('x' * 5_000_000)", timeout=30.0)
    assert len(result.stdout) < 1_000_000


# --- secrets ----------------------------------------------------------------
#
# The highest-stakes property in this module, and it was untested: this project
# runs against someone's personal API key, and the child is code the model
# wrote. Everything else here fails a demo; this one costs money and trust.


def test_parent_api_keys_are_invisible_to_the_child(monkeypatch):
    """Plant real-looking secrets in the parent and prove the child cannot see them.

    Planted rather than asserted against `_ENV_ALLOWLIST` on purpose -- checking
    the constant would only prove it equals itself, and would still pass if the
    allowlist stopped being applied.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-canary-must-not-escape")
    monkeypatch.setenv("SAMVAD_SECRET", "hmac-canary-must-not-escape")

    result = run_sandboxed(
        "import os\n"
        "print(os.environ.get('ANTHROPIC_API_KEY', 'ABSENT'))\n"
        "print(os.environ.get('SAMVAD_SECRET', 'ABSENT'))\n",
        timeout=15,
    )

    assert result.exit_code == 0
    assert "canary" not in result.stdout
    assert result.stdout.split() == ["ABSENT", "ABSENT"]


def test_the_child_environment_is_an_allowlist_not_a_denylist(monkeypatch):
    """A denylist leaks every variable nobody thought to name.

    Dumps the child's whole environment and asserts nothing unexpected survived,
    so a future variable -- an API key under a name that does not exist yet --
    is excluded by default rather than by having been remembered.
    """
    monkeypatch.setenv("SOME_FUTURE_CREDENTIAL", "canary-nobody-thought-to-block")

    result = run_sandboxed(
        "import os\nprint('\\n'.join(sorted(os.environ)))\n",
        timeout=15,
    )

    assert result.exit_code == 0
    assert "SOME_FUTURE_CREDENTIAL" not in result.stdout

    # The property is that nothing crosses over FROM THE PARENT except the
    # allowlist -- not that the child has no environment at all. The sandbox
    # injects its own hardening vars (PYTHONDONTWRITEBYTECODE), and those are
    # fine; they did not come from the parent and carry nothing.
    crossed_over = set(result.stdout.split()) & set(os.environ)
    leaked = crossed_over - set(_ENV_ALLOWLIST)
    assert not leaked, f"parent vars reached the child off-allowlist: {sorted(leaked)}"
