# OWNER: P4 -- see docs/WORK.md
"""One command for the whole demo. Start it, press numbers, talk.

    python scripts/demo.py              # local models via Ollama
    python scripts/demo.py --mock       # fixed replies, no inference, instant

It starts all four nodes (each in its own window on Windows, so the audience
sees four machines), then gives you a menu that walks the beats in order. The
menu prints what to SAY as well as what it did, so nobody has to remember a
runbook while standing up.

Why a menu and not a script that runs itself: a demo is paced by the room, not
by a timer. You press 4 when you are ready to kill a peer, not eleven seconds
after beat 3.

Everything it sends goes through scripts/demo_drive.py, so there is one
definition of a demo message. Quitting stops every node it started.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

# Imported after the sys.path lines above, which is the point of them.
from demo_drive import send

from samvad.node import load_config

#: Windows gives each node its own console window. The four terminals ARE the
#: demo's first claim -- four separate machines, nothing shared.
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

#: How long to wait for a killed peer to be noticed before giving up on saying
#: anything useful. Measured at ~26s on Windows loopback: PROBE_TIMEOUT is 5s
#: and three consecutive failures are required.
DEATH_WAIT_SECONDS = 45

TASK = "Implement binary search over a sorted list"


class Demo:
    """The four nodes and what we do to them."""

    def __init__(self, config: str, secret: str, mock: bool, windows: bool) -> None:
        self.config = config
        self.secret = secret
        self.mock = mock
        self.windows = windows
        self.peers, _, self.max_depth = load_config(config if Path(config).exists() else None)
        self.procs: dict[str, subprocess.Popen[bytes]] = {}

    # --- processes --------------------------------------------------------

    def env_for_node(self) -> dict[str, str]:
        env = dict(os.environ)
        env["SAMVAD_SECRET"] = self.secret
        env["MOCK_LLM"] = "1" if self.mock else "0"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def start(self, name: str) -> None:
        """Start one node. Always with --db: without it a restart replays
        nothing, and beat 5 is the whole point of having a log."""
        if name in self.procs and self.procs[name].poll() is None:
            print(f"  {name} is already running")
            return
        # Logs live in .demo/, not the repo root -- they are runtime state, and
        # *.db is gitignored so they never reach a commit either way.
        db_dir = ROOT / ".demo"
        db_dir.mkdir(exist_ok=True)
        cmd = [sys.executable, "-u", "-m", "samvad.node",
               "--config", self.config, "--as", name,
               "--db", str(db_dir / f"{name}.db")]
        self.procs[name] = subprocess.Popen(
            cmd, env=self.env_for_node(), cwd=str(ROOT),
            creationflags=NEW_CONSOLE if self.windows else 0,
        )
        print(f"  started {name}  (pid {self.procs[name].pid})")

    def start_all(self) -> None:
        # a last: the others must be listening before it is asked to talk
        for name in ("agent_b", "agent_c", "agent_d", "agent_a"):
            if name in self.peers:
                self.start(name)
                time.sleep(0.8)
        print("\n  waiting for all four to answer /health ...")
        for _ in range(20):
            time.sleep(1)
            if all(self.health(n) is not None for n in self.peers):
                print("  all four are up.\n")
                self.say("Four separate processes. Four clocks. Nothing shared "
                         "-- every message you will see crosses a socket.")
                return
        print("  WARNING: not all four answered. Check their windows before starting.\n")

    def stop_all(self) -> None:
        for name, proc in self.procs.items():
            if proc.poll() is None:
                proc.terminate()
                print(f"  stopped {name}")

    # --- inspection -------------------------------------------------------

    def health(self, name: str) -> dict[str, Any] | None:
        cfg = self.peers[name]
        url = f"http://{cfg['host']}:{cfg['port']}/health"
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return dict(json.load(r))
        except (urllib.error.URLError, TimeoutError, ValueError):
            return None

    def show_health(self) -> None:
        for name in self.peers:
            h = self.health(name)
            if h is None:
                print(f"  {name:<9} DOWN")
            else:
                print(f"  {name:<9} up   lamport={h.get('lamport'):<4} "
                      f"children={h.get('children')}")

    def peer_seen_down(self, watcher: str, target: str) -> bool:
        """Has `watcher` published that `target` is down? Read from its own
        event stream, which is what the dashboard reads too.

        `read1`, not `read`: /events never closes, so asking for a fixed number
        of bytes blocks until the timeout and then throws away everything it
        had already received. read1 returns whatever one syscall gives.
        """
        cfg = self.peers[watcher]
        url = f"http://{cfg['host']}:{cfg['port']}/events"
        chunks: list[bytes] = []
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                deadline = time.time() + 1.5
                while time.time() < deadline:
                    chunk = r.read1(8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        text = b"".join(chunks).decode("utf-8", "replace").replace(" ", "")
        return f'"agent":"{target}","up":false' in text

    # --- the beats --------------------------------------------------------

    def say(self, line: str) -> None:
        print(f"\n  SAY: \"{line}\"\n")

    def beat_task(self) -> None:
        send("task", sender="agent_a", to="agent_b", task=TASK,
             peers=self.peers, secret=self.secret, max_depth=self.max_depth)
        self.say("Point at the Lamport column. The four wall clocks disagree; "
                 "that number is what keeps the log in order.")

    def children_of(self, name: str) -> int:
        h = self.health(name)
        return int(h.get("children", 0)) if h else 0

    def beat_spawn(self, fan_out: int = 3) -> None:
        send("spawn", sender="agent_a", to="agent_b", task=TASK, fan_out=fan_out,
             peers=self.peers, secret=self.secret, max_depth=self.max_depth)

        # Wait for the acks to land before saying anything. The children only
        # exist for agent_a once their spawn_acks arrive, and killing agent_b
        # before that leaves nothing to reparent -- beat 4 then does nothing
        # and looks broken.
        for _ in range(10):
            time.sleep(0.5)
            n = self.children_of("agent_a")
            if n >= fan_out:
                print(f"  agent_a is now tracking {n} children on agent_b")
                break
        else:
            print(f"  WARNING: agent_a tracks {self.children_of('agent_a')} children, "
                  f"expected {fan_out}. Do not kill yet -- send this beat again.")
            return

        self.say("The parent's budget was divided among the children and sums "
                 "to the parent's. A branch that cannot afford one call cannot "
                 "spawn -- recursion ends by running out of money.")

    def beat_kill(self, target: str = "agent_b") -> None:
        proc = self.procs.get(target)
        if proc is None or proc.poll() is not None:
            print(f"  {target} is not running.")
            return
        if self.children_of("agent_a") == 0:
            print("  agent_a is tracking no children -- there is nothing to "
                  "reparent yet. Run beat 3 first, then kill.")
            return
        proc.terminate()
        t0 = time.time()
        print(f"  killed {target}. Watching agent_a notice ...")
        self.say("This takes about half a minute. Three consecutive failed "
                 "probes are required, because one dropped packet on wifi is "
                 "not a dead laptop. The pause is the guard working.")
        while time.time() - t0 < DEATH_WAIT_SECONDS:
            if self.peer_seen_down("agent_a", target):
                print(f"  agent_a marked {target} down after "
                      f"{time.time() - t0:.0f}s")
                self.say("Orphans reparented to the grandparent. In-flight "
                         "results still arrive and the task completes. There is "
                         "nothing to kill in a single-process demo.")
                return
            time.sleep(2)
        print(f"  no down signal within {DEATH_WAIT_SECONDS}s -- read agent_a's "
              f"window for 'reparented N orphan(s)'.")

    def beat_restart(self, target: str = "agent_b") -> None:
        self.start(target)
        time.sleep(3)
        print(f"  {target} restarted on the same --db.")
        self.say("It replayed its log and resumed its clock -- look for "
                 "'replayed N messages; lamport resumes at M' in its window. A "
                 "node that restarts at zero reorders its own history.")


MENU = """
  ------------------------------------------------------------------
   1  start all four nodes          5  restart agent_b   (beat 5)
   2  send one task     (beat 2)    6  health of all four
   3  fan out to 3      (beat 3)    7  open the dashboard
   4  kill agent_b      (beat 4)    q  stop everything and quit

   a  the whole demo: 1 2 3 4 5, pausing before each beat
  ------------------------------------------------------------------
  one number, or several -- "1,2,3" and "12345" both work"""

#: `a` is the whole run. Beat 1 is included so one keystroke covers a cold start.
WHOLE_DEMO = ("1", "2", "3", "4", "5")


def parse_choice(text: str) -> list[str]:
    """Menu keys from whatever was typed.

    "3" -> ["3"] · "1,2,3" -> ["1","2","3"] · "12345" -> [...] · "q" -> ["q"]

    People type the sequence they were told to press, commas and all. Rejecting
    that and printing a terse hint is how someone ends up pasting "1,2,3,4,5"
    into cmd.exe by mistake -- which is exactly what happened in rehearsal.
    """
    return re.findall(r"\d|[a-z]+", text.strip().lower())


def main() -> None:
    p = argparse.ArgumentParser(prog="python scripts/demo.py",
                                description="Run the whole Samvad demo from one menu.")
    p.add_argument("--config", default="config/peers.yaml")
    p.add_argument("--secret", default=os.environ.get("SAMVAD_SECRET") or "samvad-demo",
                   help="shared HMAC secret; every node this starts gets the same one")
    p.add_argument("--mock", action="store_true",
                   help="MOCK_LLM=1 -- fixed replies, no inference. The fallback.")
    p.add_argument("--no-windows", action="store_true",
                   help="do not open a console per node (output lands in this one)")
    a = p.parse_args()

    demo = Demo(a.config, a.secret, a.mock, windows=not a.no_windows)
    print(f"\n  Samvad demo  --  {'MOCK (no inference)' if a.mock else 'Ollama'}"
          f"  --  secret {a.secret!r}")

    # A previous run that was closed by its window rather than by 'q' leaves
    # nodes holding these ports. Starting anyway gives four windows that die on
    # "address already in use" and a menu driving nothing -- so say it here,
    # before anyone presses 1 in front of a room.
    busy = [n for n in demo.peers if demo.health(n) is not None]
    if busy:
        print(f"\n  WARNING: {', '.join(busy)} already answer -- an earlier demo is "
              f"still running.\n  Close those windows first, or press 6 to see them. "
              f"Pressing 1 now will fail.")
    print("  Start with 1. Then 2, 3, 4, 5 in order. Read what it tells you to say.")

    def run_one(key: str) -> bool:
        """Run one menu key. False means quit."""
        if key == "1":
            demo.start_all()
        elif key == "2":
            demo.beat_task()
        elif key == "3":
            demo.beat_spawn()
        elif key == "4":
            demo.beat_kill()
        elif key == "5":
            demo.beat_restart()
        elif key == "6":
            demo.show_health()
        elif key == "7":
            cfg = demo.peers["agent_a"]
            url = f"http://{cfg['host']}:{cfg['port']}/"
            print(f"  opening {url} -- confirm the corner reads 'live'")
            import webbrowser
            webbrowser.open(url)
        elif key in ("q", "quit", "exit"):
            return False
        else:
            print(f"  {key!r} is not on the menu. Type a number 1-7, "
                  f"or 'a' for the whole demo, or 'q' to quit.")
        return True

    def pause(next_key: str) -> str:
        """Hold before the next beat. The room sets the pace, not a timer.

        Returns "go", "skip" or "stop".
        """
        try:
            answer = input(f"\n  ready for step {next_key}? "
                           f"[Enter to go, s to skip, q to stop]  ").strip().lower()
        except EOFError:
            return "go"
        if answer.startswith("q"):
            return "stop"
        if answer.startswith("s"):
            return "skip"
        return "go"

    try:
        while True:
            print(MENU)
            try:
                typed = input("  > ")
            except EOFError:
                break

            keys = parse_choice(typed)
            if not keys:
                continue
            if keys == ["a"]:
                keys = list(WHOLE_DEMO)

            for i, key in enumerate(keys):
                if len(keys) > 1 and i > 0:
                    decision = pause(key)
                    if decision == "stop":
                        return
                    if decision == "skip":
                        print(f"  skipped {key}")
                        continue
                if not run_one(key):
                    return
    finally:
        print("\n  stopping nodes ...")
        demo.stop_all()
        print("  done.\n")


if __name__ == "__main__":
    main()
