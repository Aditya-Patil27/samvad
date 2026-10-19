# OWNER: P4 -- see docs/WORK.md
"""Send one signed message into a running node. The demo's remote control.

`python -m samvad.node --task ...` opens a conversation at startup and that is
all it can do. A fan-out needs a `spawn_request` carrying `payload.fan_out`, and
nothing else in the repo sends one -- so beats 3 and 4 of docs/DEMO.md, the
fan-out and the kill-a-peer, had no trigger. This is that trigger.

    python scripts/demo_drive.py task  --to agent_b
    python scripts/demo_drive.py spawn --to agent_b --fan-out 3

Then kill agent_b and watch the grandparent's terminal reparent its orphans.

WHAT THIS IS NOT: a back door. It signs with the same `SAMVAD_SECRET` every
peer shares and speaks as `--from`, which the receiver accepts because HMAC
proves *possession of the secret*, not identity. That is the honest answer to
"what if an agent lies?" in docs/DEMO.md -- this script is a small live
demonstration of it, and it only works because you already hold the key.

The Lamport value is read from the sender's own `/health` and incremented, so
an injected message does not land in the log looking older than the history it
follows. Pass --lamport to override.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _ensure_deps() -> None:
    """Re-run under the project's venv if this interpreter lacks the deps.

    `python scripts/demo.py` from a plain cmd.exe picks whichever Python is on
    PATH, which is usually not the one holding httpx and fastapi. The failure
    is a traceback about a missing module, and the last place anyone wants to
    debug an interpreter is in front of a panel. So: find the venv and switch
    to it, out loud.

    scripts/demo.py imports this module before it imports samvad, so this
    covers both entry points from one place.
    """
    try:
        import httpx  # noqa: F401  -- probe only
        return
    except ModuleNotFoundError:
        pass

    if os.environ.get("SAMVAD_REEXEC") == "1":       # already tried; do not loop
        sys.exit("The venv is missing dependencies. Run:  uv sync --extra dev")

    for candidate in (ROOT / ".venv" / "Scripts" / "python.exe",
                      ROOT / ".venv" / "bin" / "python"):
        if candidate.exists():
            print(f"  switching to {candidate}", flush=True)
            env = dict(os.environ, SAMVAD_REEXEC="1")
            # subprocess, NOT os.execv. On Windows execv does not replace the
            # process -- it spawns a child and the parent exits, so cmd.exe
            # prints its prompt and starts reading the same keyboard the child
            # is reading. Menu keys then land in the shell: "'2' is not
            # recognized as an internal or external command". Waiting on a
            # child keeps exactly one reader of the console.
            completed = subprocess.run([str(candidate), *sys.argv], env=env, check=False)
            sys.exit(completed.returncode)

    sys.exit("No .venv found and httpx is missing. Run:  uv sync --extra dev")


_ensure_deps()

from samvad import security
from samvad.node import load_config, new_message
from samvad.protocol import Performative

TIMEOUT = 10.0


def endpoint(peers: dict[str, Any], address: str) -> tuple[str, int]:
    """Host and port of the peer owning `address`.

    Children are never in the peer table -- `agent_b/worker_1` is reached
    through `agent_b`, which is what the address prefix is for.
    """
    peer = address.split("/")[0]
    if peer not in peers:
        sys.exit(f"{peer!r} is not in the peer table: {sorted(peers)}")
    cfg = peers[peer]
    return str(cfg["host"]), int(cfg["port"])


def current_lamport(host: str, port: int) -> int:
    """The sender's clock, or 0 if it is not up. Never fatal -- a wrong guess
    orders one message badly; refusing to send kills the demo beat."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3) as r:
            return int(json.load(r).get("lamport", 0))
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError):
        return 0


def post(url: str, body: dict[str, Any]) -> tuple[int, str]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except urllib.error.URLError as e:
        sys.exit(f"{url} unreachable: {e.reason}. Is that node running?")


def send(kind: str, *, sender: str, to: str, task: str, fan_out: int = 3,
         peers: dict[str, Any], secret: str, max_depth: int = 3,
         lamport: int | None = None, conversation: str | None = None) -> int:
    """Sign and post one message. Returns the HTTP status.

    Shared with scripts/demo.py so there is one implementation of what a
    demo-driven message looks like, not two that drift apart.
    """
    host, port = endpoint(peers, to)
    if lamport is None:
        from_host, from_port = endpoint(peers, sender)
        lamport = current_lamport(from_host, from_port) + 1

    if kind == "spawn":
        performative = Performative.SPAWN_REQUEST
        payload: dict[str, Any] = {"fan_out": max(1, fan_out), "subtask": task}
    else:
        performative = Performative.TASK_REQUEST
        payload = {"subtask": task, "constraints": []}

    msg = new_message(sender, to, performative, task, lamport=lamport,
                      payload=payload, conversation_id=conversation,
                      max_depth=max_depth)
    msg.sig = security.sign(msg, secret)

    status, body = post(f"http://{host}:{port}/message", msg.model_dump(mode="json"))
    print(f"{performative.value} {sender} -> {to}  lamport={lamport}  "
          f"conversation={msg.conversation_id}")
    print(f"  {status} {body}")
    return status


def main() -> None:
    p = argparse.ArgumentParser(
        prog="python scripts/demo_drive.py",
        description="Send one signed task_request or spawn_request to a running node.",
    )
    p.add_argument("kind", choices=["task", "spawn"])
    p.add_argument("--config", default="config/peers.yaml", help="peer table")
    p.add_argument("--from", dest="sender", default="agent_a", help="who the message is from")
    p.add_argument("--to", default="agent_b", help="who it goes to")
    p.add_argument("--task", default="Implement binary search over a sorted list",
                   help="the root_task -- immutable for the whole conversation")
    p.add_argument("--fan-out", type=int, default=3, help="children to ask for (spawn only)")
    p.add_argument("--lamport", type=int, help="override the clock read from --from's /health")
    p.add_argument("--conversation", help="join an existing conversation instead of opening one")
    a = p.parse_args()

    secret = os.environ.get("SAMVAD_SECRET", "")
    if not secret:
        print("warning: SAMVAD_SECRET is empty -- this will only be accepted by "
              "nodes started without one either", file=sys.stderr)

    peers, _, max_depth = load_config(a.config if Path(a.config).exists() else None)

    status = send(a.kind, sender=a.sender, to=a.to, task=a.task, fan_out=a.fan_out,
                  peers=peers, secret=secret, max_depth=max_depth,
                  lamport=a.lamport, conversation=a.conversation)

    # 202 is the only success. A 401 means the secrets differ, which is the
    # single most common reason a demo node silently ignores everything.
    if status != 202:
        sys.exit(1)


if __name__ == "__main__":
    main()
