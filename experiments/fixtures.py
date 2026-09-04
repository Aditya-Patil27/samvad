# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Synthetic message log generator.

BUILD THIS FIRST, in week 1. P4 has nothing real to observe until week 2, so
the dashboard and every experiment get built against fake data and swapped
onto the real log later. Do not sit and wait.

Emits plain dicts in the frame format `/events` carries -- `{"type": "node"}`
and `{"type": "message"}`, per the HTML comment atop dashboard/index.html.
Message frames also carry every required envelope field from docs/PROTOCOL.md,
so once protocol.py exists the swap is one line:

    Message(**{k: v for k, v in frame.items() if k not in DISPLAY_ONLY})

**This module deliberately does not import `samvad.protocol`.** That module is
a frozen week-1 joint task and does not exist yet; a fixture that needs the real
envelope to make fake data arrives too late to unblock anyone.

`scripted_run()` is the 16-step scenario ported from `demo()` in
dashboard/index.html -- a budget-sliced fan-out, then a peer dying mid-flight
with its orphans reparented. Python is now the single source of that scenario.
`synthetic_log()` replays both moments seeded and parameterised, for the five
experiments.

**Order by `lamport`, never by `timestamp`.** Both generators stamp wall-clock
times with a fixed per-device skew, so `sorted(key=timestamp)` returns a
*different* order from `by_lamport()`. That disagreement is deliberate: fake
data that agreed would quietly bless code that sorts by wall clock, and the bug
would first appear on real hardware, during the demo.
"""
from __future__ import annotations

import hashlib
import json
import string
import uuid
from datetime import UTC, datetime, timedelta
from random import Random
from typing import Any

PROTOCOL_VERSION = "2.0"
CONTEXT_LIMIT = 200_000

#: The four lanes the dashboard draws. Order is the column order.
PEERS: tuple[str, ...] = ("agent_a", "agent_b", "agent_c", "agent_d")

#: Display hints, not protocol fields. They drop out at the Message boundary.
DISPLAY_ONLY = ("type", "child", "note", "dashed")

#: USD per 1M tokens (input, output). Local models are free, which is the point
#: of measurement 5 running one.
MODEL_RATES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (15.0, 75.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "ollama:qwen2.5-coder": (0.0, 0.0),
}

#: Per-peer models, transcribed from demo() in dashboard/index.html.
PEER_MODELS: dict[str, str] = {
    "agent_a": "claude-opus-5", "agent_b": "claude-opus-5",
    "agent_c": "ollama:qwen2.5-coder", "agent_d": "claude-haiku-4-5",
}

#: Matches the example timestamp in docs/PROTOCOL.md.
_EPOCH = datetime(2026, 8, 24, 10, 15, tzinfo=UTC)

#: Seconds of clock skew between adjacent peers: big enough that wall-clock and
#: Lamport order visibly disagree, small enough to be believable on a LAN where
#: nobody ran ntpd.
_SKEW_STEP = 1.7

_EVIDENCE = ("test_output", "file_content", "peer_report", "model_prior")

#: Per-role evidence mix. Planners reason about work that has not happened yet
#: so they cite `model_prior`; executors have test output to point at.
#: docs/EXPERIMENTS.md#3 expects exactly this shape.
_EVIDENCE_WEIGHTS: dict[str, tuple[float, ...]] = {
    "planner": (0.05, 0.10, 0.15, 0.70),
    "executor": (0.50, 0.30, 0.10, 0.10),
    "reviewer": (0.30, 0.20, 0.40, 0.10),
}

_ROOT_TASKS = (
    "Implement a binary search function",
    "Add exponential backoff to the HTTP client",
    "Fix the off-by-one in the pagination cursor",
    "Write property tests for the tokeniser",
    "Port the CSV writer to streaming output",
)

_SUBTASKS = (
    "add unit tests for empty and single-element input",
    "handle the 429 response path",
    "cover the boundary at page_size exactly",
    "shrink failing cases to a minimal example",
    "avoid buffering the whole file in memory",
)

_CLAIM_TEXT: dict[str, tuple[str, str]] = {
    "test_output": ("the empty-input case is covered", "test_search.py::test_empty passed"),
    "file_content": ("the helper already exists", "read from src/util.py:41"),
    "peer_report": ("the interface is stable", "agent_c reported it in this conversation"),
    "model_prior": ("bisect_left is O(log n)", "no test ran"),
}


# --- frames: the two shapes /events carries --------------------------------

def peer_names(n_agents: int) -> list[str]:
    """`["agent_a", "agent_b", ...]`, matching PEERS for the first four."""
    if not 2 <= n_agents <= 26:
        raise ValueError(f"n_agents must be between 2 and 26, got {n_agents}")
    return [f"agent_{c}" for c in string.ascii_lowercase[:n_agents]]


def device(agent_path: str) -> str:
    """The peer a path lives on: `agent_b/worker_2/checker_1` -> `agent_b`.

    One Lamport clock per device, not per agent -- children share their host's.
    """
    return agent_path.split("/", 1)[0]


def model_for(peer: str) -> str:
    """Model assigned to a peer. Peers past the fourth cycle the same three."""
    if peer in PEER_MODELS:
        return PEER_MODELS[peer]
    return tuple(MODEL_RATES)[peer_names(26).index(peer) % len(MODEL_RATES)]


def node_frame(agent: str, **fields: Any) -> dict[str, Any]:
    """A `type: node` frame. Sparse on purpose: the dashboard's `onNode()`
    merges with `??`, so an absent key means "unchanged" -- which is what lets
    a peer go down without the same frame restating its context and budget."""
    return {"type": "node", "agent": agent, **fields}


def message_frame(
    *, lamport: int, sender: str, receiver: str, performative: str, timestamp: str,
    cost: dict[str, Any] | None = None, child: str | None = None,
    note: str | None = None, dashed: bool = False, **envelope: Any,
) -> dict[str, Any]:
    """A `type: message` frame.

    The named arguments are what dashboard/index.html reads; `**envelope`
    carries the docs/PROTOCOL.md fields, which the dashboard ignores and the
    experiments need.
    """
    return {
        "type": "message", "lamport": lamport, "timestamp": timestamp,
        "sender": sender, "receiver": receiver, "performative": performative,
        "cost": cost, "child": child, "note": note, "dashed": dashed, **envelope,
    }


def messages(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Just the message frames, in emission order."""
    return [f for f in frames if f["type"] == "message"]


def nodes(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Just the node frames, in emission order."""
    return [f for f in frames if f["type"] == "node"]


def by_lamport(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Message frames in canonical `(lamport, sender)` order -- the only correct
    way to order a Samvad log; `timestamp` is for humans and the replay window.

    Node frames are dropped: they carry no clock, so there is no honest place
    for them in a Lamport ordering.
    """
    return sorted(messages(frames), key=lambda m: (m["lamport"], m["sender"]))


# --- the ported dashboard scenario -----------------------------------------

#: Transcribed row for row from `demo()` in dashboard/index.html. Change the
#: scenario here, not there -- tests/test_fixtures.py pins the two together.
#: Columns: lamport, sender, receiver, performative, then optional
#: usd / child / note / dashed / down.
_DEMO_SCRIPT: tuple[dict[str, Any], ...] = (
    {"lam": 12, "s": "agent_a", "r": "agent_b", "p": "task_request", "usd": 0.0031},
    {"lam": 13, "s": "agent_a", "r": "agent_d", "p": "task_request", "usd": 0.0028},
    {"lam": 15, "s": "agent_b", "r": "agent_b", "p": "spawn_ack", "usd": 0.0012,
     "child": "b/worker_1"},
    {"lam": 16, "s": "agent_b", "r": "agent_b", "p": "spawn_ack", "usd": 0.0012,
     "child": "b/worker_2"},
    {"lam": 18, "s": "agent_d", "r": "agent_a", "p": "task_result", "usd": 0.0009},
    {"lam": 20, "s": "agent_b", "r": "agent_a", "p": "task_result", "usd": 0.0044},
    {"lam": 21, "s": "agent_a", "r": "agent_c", "p": "task_request", "usd": 0.0007},
    {"lam": 23, "s": "agent_c", "r": "agent_c", "p": "spawn_ack",
     "child": "c/verifier_1 correctness"},
    {"lam": 24, "s": "agent_c", "r": "agent_c", "p": "spawn_ack", "child": "c/verifier_2 edges"},
    {"lam": 25, "s": "agent_c", "r": "agent_c", "p": "spawn_ack",
     "child": "c/verifier_3 execution"},
    {"lam": 28, "s": "agent_c", "r": "agent_a", "p": "task_result", "usd": 0.0021},
    {"lam": 30, "s": "agent_a", "r": "agent_b", "p": "task_request", "usd": 0.0033},
    {"lam": 31, "s": "agent_b", "r": "agent_b", "p": "task_result",
     "note": "✕ AGENT_B DOWN", "down": "agent_b"},
    {"lam": 33, "s": "agent_b", "r": "agent_a", "p": "child_result", "dashed": True,
     "note": "orphans reparented"},
    {"lam": 35, "s": "agent_a", "r": "agent_d", "p": "task_request", "usd": 0.0026},
    {"lam": 38, "s": "agent_d", "r": "agent_a", "p": "task_result", "usd": 0.0018},
)

#: The dashboard stepper's interval, so ported timestamps advance at the pace
#: the animation implies.
_DEMO_INTERVAL = 0.62


def scripted_run() -> list[dict[str, Any]]:
    """The dashboard's hand-written 16-step run, as frames.

    Ported from `demo()`, including the node frames its stepper derives after
    each message -- so this list alone can drive the page. Fixed script, no
    seed: it is a demo, not a sample. The two moments it exists to show are
    lamport 23-25 (agent_c fans out to a three-lens verifier panel) and lamport
    31-33 (agent_b dies mid-flight, orphans reparent to agent_a).
    """
    skew = _skews(PEERS)
    state = {p: {"used": 2000, "children": 0, "budget": 0.5} for p in PEERS}
    frames = [
        node_frame(p, model=model_for(p), up=True, children=0,
                   context={"used": 2000, "limit": CONTEXT_LIMIT}, budget_usd=0.5)
        for p in PEERS
    ]
    for i, step in enumerate(_DEMO_SCRIPT, start=1):
        peer = device(step["s"])
        cost = None
        if "usd" in step:
            cost = {"input": 0, "output": 0, "cache_read": 0,
                    "usd": step["usd"], "model": model_for(peer)}
        frames.append(message_frame(
            lamport=step["lam"], sender=step["s"], receiver=step["r"],
            performative=step["p"], cost=cost, child=step.get("child"),
            timestamp=_iso(_DEMO_INTERVAL * (i - 1) + skew[peer]),
            note=step.get("note"), dashed=step.get("dashed", False),
        ))
        if "down" in step:  # the stepper's derived node frame, arithmetic and all
            frames.append(node_frame(step["down"], up=False, children=0))
            state[step["down"]]["children"] = 0
            continue
        n = state[peer]
        n["children"] += 1 if step.get("child") else 0
        n["used"] += 3200 + i * 900
        n["budget"] = max(0.0, n["budget"] - step.get("usd", 0.0) * 12)
        frames.append(node_frame(
            peer, children=n["children"], budget_usd=round(n["budget"], 6),
            context={"used": n["used"], "limit": CONTEXT_LIMIT}))
    return frames


# --- the parameterised generator -------------------------------------------

def synthetic_log(
    n_messages: int = 200, n_agents: int = 4, *,
    seed: int = 0, fan_out: int = 3, max_depth: int = 2,
) -> list[dict[str, Any]]:
    """Plausible message log: correct Lamport ordering, realistic costs,
    a mix of evidence types, some spawn trees.

    Deterministic for a given `(n_messages, n_agents, seed, fan_out, max_depth)`.
    Reproducibility is not a nicety: an experiment measured against a fixture
    that drifts between runs has measured nothing.

    Returns exactly `n_messages` message frames, node frames interleaved. The
    run replays both moments from `scripted_run()`: budget-sliced fan-outs
    throughout, and one peer dying about 55% of the way in with its orphans
    reparented onto the coordinator. Below ~40 messages the log is truncated
    before the death, so ask for more than that if you need it.
    """
    if n_messages < 1:
        raise ValueError(f"n_messages must be >= 1, got {n_messages}")
    if fan_out < 1:
        raise ValueError(f"fan_out must be >= 1, got {fan_out}")
    if max_depth < 1:
        raise ValueError(f"max_depth must be >= 1, got {max_depth}")

    peers = peer_names(n_agents)  # validates n_agents
    run = _Run(peers, seed=seed, fan_out=fan_out, max_depth=max_depth)
    run.open()

    coordinator, workers = peers[0], peers[1:]
    death_at = max(1, int(n_messages * 0.55))
    died = False
    i = 0
    while run.n_messages < n_messages:
        worker = workers[i % len(workers)]
        if not died and run.n_messages >= death_at:
            run.death_scene(coordinator, worker)
            died = True
        else:
            run.fan_out_scene(coordinator, worker)
        i += 1
    return _truncate(run.frames, n_messages)


def _truncate(frames: list[dict[str, Any]], n_messages: int) -> list[dict[str, Any]]:
    """Cut to exactly `n_messages` message frames. A log is a slice of a run
    that was still going, so an arbitrary tail is the honest shape."""
    kept: list[dict[str, Any]] = []
    seen = 0
    for f in frames:
        if f["type"] == "message":
            if seen == n_messages:
                break
            seen += 1
        kept.append(f)
    return kept


def _iso(offset_seconds: float) -> str:
    """ISO 8601 UTC, `offset_seconds` after the epoch."""
    return (_EPOCH + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def _skews(peers: tuple[str, ...] | list[str]) -> dict[str, float]:
    """Fixed per-device clock skew, centred on zero. Four laptops, four clocks,
    nobody running ntpd -- this is what makes wall order disagree with Lamport."""
    mid = (len(peers) - 1) / 2
    return {p: round((i - mid) * _SKEW_STEP, 3) for i, p in enumerate(peers)}


def _spend(budget: dict[str, Any], usd: float) -> dict[str, Any]:
    """Decrement before the call, never after -- and never below zero."""
    return {"usd_remaining": round(max(0.0, budget["usd_remaining"] - usd), 6),
            "turns_remaining": max(0, budget["turns_remaining"] - 1)}


class _Run:
    """Builder for one synthetic run. Holds a Lamport clock per device."""

    def __init__(self, peers: list[str], *, seed: int, fan_out: int, max_depth: int) -> None:
        self.rng = Random(seed)
        self.peers = peers
        self.fan_out = fan_out
        self.max_depth = max_depth
        self.frames: list[dict[str, Any]] = []
        self.n_messages = 0
        self.skew = _skews(peers)
        self.elapsed = 0.0
        self.conversations = 0
        # Clocks start apart: these nodes have been up a while, and four devices
        # that agreed on a clock would be the one thing Lamport is not for.
        self.lamport = {p: self.rng.randrange(3, 15) for p in peers}
        self.used = {p: self.rng.randrange(1_800, 9_000) for p in peers}
        self.children = dict.fromkeys(peers, 0)

    def open(self) -> None:
        """Opening node frames, one per peer, so the strip renders."""
        for p in self.peers:
            self.node(p, model=model_for(p), up=True, children=0, budget_usd=0.5,
                      context={"used": self.used[p], "limit": CONTEXT_LIMIT})

    def node(self, peer: str, **fields: Any) -> None:
        self.frames.append(node_frame(peer, **fields))

    def send(
        self, sender: str, receiver: str, performative: str, *, depth: int,
        cost: dict[str, Any] | None = None, child: str | None = None,
        note: str | None = None, dashed: bool = False, **envelope: Any,
    ) -> dict[str, Any]:
        """Stamp and append one message, advancing both devices' clocks.

        Lamport, per docs/PROTOCOL.md: on send `local += 1`; on receive
        `local = max(local, remote) + 1`. `depth` fills `spawn.depth`; the rest
        of the envelope (conversation_id, root_task, turn, budget, payload, and
        optionally reply_to / claims / artifacts / task_status) is passed through.
        """
        host = device(sender)
        self.lamport[host] += 1
        lamport = self.lamport[host]
        self.elapsed += round(self.rng.uniform(0.08, 1.4), 3)
        self.used[host] = min(CONTEXT_LIMIT, self.used[host] + self.rng.randrange(400, 4_200))

        frame = message_frame(
            lamport=lamport, sender=sender, receiver=receiver, performative=performative,
            timestamp=_iso(self.elapsed + self.skew[host]),
            cost=cost, child=child, note=note, dashed=dashed,
            protocol_version=PROTOCOL_VERSION, message_id=self._message_id(),
            spawn={"parent": sender.rsplit("/", 1)[0], "depth": depth,
                   "max_depth": self.max_depth},
            context={"used": self.used[host], "limit": CONTEXT_LIMIT}, sig=None,
            **{"reply_to": None, "artifacts": [], "claims": [],
               "task_status": "in_progress", **envelope},
        )
        self.frames.append(frame)
        self.n_messages += 1
        peer = device(receiver)
        self.lamport[peer] = max(self.lamport[peer], lamport) + 1
        return frame

    # -- scenes -----------------------------------------------------------

    def fan_out_scene(self, coordinator: str, worker: str) -> None:
        """One task: coordinator delegates, worker fans out, results come back."""
        conv, root = self._new_conversation()
        budget = self._root_budget()
        common = {"conversation_id": conv, "root_task": root}

        request = self.send(
            coordinator, worker, "task_request", depth=0, turn=0, budget=budget,
            payload={"subtask": self.rng.choice(_SUBTASKS),
                     "constraints": ["O(log n)", "Python 3.11", "pytest"]},
            task_status="pending", claims=self._claims("planner"), **common)
        self._spawn(worker, common, budget, depth=1, role="worker")

        cost = self._cost(model_for(device(worker)))
        budget = _spend(budget, cost["usd"])
        self.send(
            worker, coordinator, "task_result", depth=0, turn=1, budget=budget,
            reply_to=request["message_id"], cost=cost, task_status="complete",
            payload={"result": "subtask complete", "exit_code": 0},
            claims=self._claims("executor"), artifacts=[self._artifact(conv)], **common)
        self.node(worker, children=self.children[worker],
                  context={"used": self.used[worker], "limit": CONTEXT_LIMIT},
                  budget_usd=budget["usd_remaining"])

    def death_scene(self, coordinator: str, victim: str) -> None:
        """A peer dies mid-fan-out. Its children are reparented and their results
        still arrive -- P2's supervision guarantee, made visible.

        Ported from the lamport 30-33 stretch of demo(). The victim comes back up
        at the end so the run can continue; with two peers there would otherwise
        be nobody left to talk to.
        """
        conv, root = self._new_conversation()
        budget = self._root_budget()
        common = {"conversation_id": conv, "root_task": root}

        self.send(coordinator, victim, "task_request", depth=0, turn=0, budget=budget,
                  payload={"subtask": self.rng.choice(_SUBTASKS), "constraints": ["pytest"]},
                  task_status="pending", claims=self._claims("planner"), **common)
        orphans = self._spawn(victim, common, budget, depth=1, role="worker",
                              report_back=False)

        self.send(victim, victim, "task_result", depth=0, turn=1, budget=budget,
                  payload={"result": "host unreachable", "exit_code": 137},
                  task_status="abandoned", note=f"✕ {victim.upper()} DOWN", **common)
        self.node(victim, up=False, children=0)
        self.children[victim] = 0

        # The supervisor's notice, then the orphans' work arriving on their new
        # addresses under the coordinator. Both hops are drawn dashed: neither
        # travelled the path it was originally addressed on.
        self.send(victim, coordinator, "child_result", depth=0, turn=2, budget=budget,
                  payload={"result": "parent lost", "summary": f"{len(orphans)} orphans",
                           "orphans": orphans, "reparented_from": victim,
                           "reparented_to": coordinator},
                  note="orphans reparented", dashed=True,
                  claims=self._claims("reviewer"), **common)
        for i, orphan in enumerate(orphans, start=1):
            cost = self._cost(model_for(coordinator))
            budget = _spend(budget, cost["usd"])
            self.send(
                f"{coordinator}/worker_{i}", coordinator, "child_result", depth=1,
                turn=2 + i, budget=budget, cost=cost, task_status="complete", dashed=True,
                payload={"result": "subtask complete", "summary": "resumed after reparent",
                         "exit_code": 0, "was": orphan, "reparented_from": victim,
                         "reparented_to": coordinator},
                claims=self._claims("reviewer"), **common)
        self.children[coordinator] += len(orphans)
        self.node(coordinator, children=self.children[coordinator],
                  context={"used": self.used[coordinator], "limit": CONTEXT_LIMIT},
                  budget_usd=budget["usd_remaining"])
        # Restarted, log replayed, back in the peer table.
        self.node(victim, up=True, children=0, budget_usd=budget["usd_remaining"],
                  context={"used": self.used[victim], "limit": CONTEXT_LIMIT})

    def _spawn(
        self, parent: str, common: dict[str, str], parent_budget: dict[str, Any],
        *, depth: int, role: str, report_back: bool = True,
    ) -> list[str]:
        """Fan `parent` out to `fan_out` children and collect their results.

        **Budget conservation.** Each slice is `parent / (fan_out + 1)`, so the
        slices sum to strictly less than the parent's and the parent keeps a
        remainder. A branch that cannot afford one call is refused rather than
        spawned; that refusal and the depth cap are the only things that stop
        the recursion, and a fixture that faked either would let a downstream
        check pass against impossible input.
        """
        refusal = None
        share = self.fan_out + 1
        slice_usd = int(parent_budget["usd_remaining"] * 1_000_000) // share / 1_000_000
        slice_turns = parent_budget["turns_remaining"] // share
        if depth > self.max_depth:
            refusal = "max_depth"
        elif slice_turns < 1 or slice_usd <= 0.0:
            refusal = "budget_policy"
        if refusal:
            # Both are protocol states, not a silent return -- see PROTOCOL.md.
            self.send(parent, parent, "spawn_refused", depth=depth - 1, turn=1,
                      budget=parent_budget,
                      payload={"reason": refusal,
                               "spent": parent_budget["usd_remaining"]}, **common)
            return []

        host = device(parent)
        acks = [
            (f"{parent}/{role}_{j}", self.send(
                parent, parent, "spawn_ack", depth=depth - 1, turn=1, budget=parent_budget,
                child=f"{host}/{role}_{j}",
                payload={"agent_path": f"{parent}/{role}_{j}",
                         "slice": {"usd_remaining": slice_usd,
                                   "turns_remaining": slice_turns, "depth": depth}},
                **common))
            for j in range(1, self.fan_out + 1)
        ]
        self.children[host] += self.fan_out
        child_budget = {"usd_remaining": slice_usd, "turns_remaining": slice_turns}
        paths = [path for path, _ in acks]
        if not report_back:
            return paths

        for index, (path, ack) in enumerate(acks):
            if index == 0:
                # One branch goes deeper, so a log carries a real spawn tree
                # rather than a flat star. Widening every branch would multiply
                # the message count by fan_out per level for no extra signal.
                self._spawn(path, common, child_budget, depth=depth + 1, role="checker")
            cost = self._cost(model_for(host))
            self.send(path, parent, "child_result", depth=depth, turn=2, cost=cost,
                      budget=_spend(child_budget, cost["usd"]), reply_to=ack["message_id"],
                      payload={"result": "branch done", "summary": "40 lines, tests green",
                               "exit_code": 0},
                      task_status="complete", claims=self._claims("reviewer"), **common)
        return paths

    # -- deterministic detail ---------------------------------------------

    def _root_budget(self) -> dict[str, Any]:
        """A root budget that survives being sliced `max_depth` times.

        Slices are `parent / (fan_out + 1)` per level, so a flat 12 turns would
        leave level two unable to afford a call and every log would be a
        one-level star. Sized so the deepest level gets one turn: deep enough to
        be a tree, tight enough that conservation is still what ends it.
        """
        return {"usd_remaining": 0.5,
                "turns_remaining": min(1024, (self.fan_out + 1) ** self.max_depth)}

    def _new_conversation(self) -> tuple[str, str]:
        self.conversations += 1
        return f"conv-{self.conversations:04x}", self.rng.choice(_ROOT_TASKS)

    def _message_id(self) -> str:
        """A uuid4-shaped id from the seeded RNG, so two runs at one seed
        compare equal field for field."""
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def _claims(self, role: str) -> list[dict[str, str]]:
        return [
            {"claim": _CLAIM_TEXT[k][0], "evidence": _CLAIM_TEXT[k][1], "evidence_type": k}
            for k in self.rng.choices(_EVIDENCE, weights=_EVIDENCE_WEIGHTS[role], k=2)
        ]

    def _artifact(self, salt: str) -> dict[str, Any]:
        digest = hashlib.sha256(f"{salt}:{self.rng.getrandbits(64)}".encode()).hexdigest()
        return {"ref": f"sha256:{digest}", "kind": "python_source",
                "summary": "binary search impl, 40 lines",
                "tokens": self.rng.randrange(120, 900)}

    def _cost(self, model: str) -> dict[str, Any]:
        rate_in, rate_out = MODEL_RATES[model]
        inp = self.rng.randrange(1_500, 9_000)
        out = self.rng.randrange(200, 1_800)
        cache = self.rng.randrange(0, inp)
        usd = ((inp - cache) * rate_in + cache * rate_in * 0.1 + out * rate_out) / 1_000_000
        return {"input": inp, "output": out, "cache_read": cache,
                "usd": round(usd, 6), "model": model}


if __name__ == "__main__":
    # JSONL on stdout, one frame per line -- the shape /events pushes, so it can
    # be replayed into the dashboard or piped into an experiment.
    for _frame in synthetic_log():
        print(json.dumps(_frame))
