# Samvad — Instructions for AI Assistants

Read this before touching anything. Four people work in this repo, each with an AI assistant. This file is how all four assistants stay aligned on one contract.

---

## What this is

A distributed multi-agent system. Four permanent peer agents, one per device, on the same LAN, exchanging signed JSON messages over HTTP. Each peer can spawn ephemeral child agents locally or on a peer's device.

Design doc: [Samvad.md](Samvad.md) · Protocol: [docs/PROTOCOL.md](docs/PROTOCOL.md) · Ownership: [docs/WORK.md](docs/WORK.md)

**Every agent runs the same binary.** Config and system prompt differ; code does not. There is no "Agent A implementation" — if you are about to write one, stop.

---

## State of the repo

Scaffolded, not built. Every module under `src/samvad/` raises `NotImplementedError` and carries an
`OWNER:` header plus the invariants it must not break — read that header before editing the file.

Already working, do not rewrite: `scripts/netcheck.py`, `dashboard/index.html`, packaging, CI,
`store/schema.sql`, `prompts/*.md`.

`protocol.py` holds only the enums and a field-by-field TODO. **Do not fill it in.** It is a
deliberate week-1 task for the four humans together. If asked to write it, say so and stop.

---

## Hard rules

**1 — Do not edit files outside your owner's layer.**

| Owner | Owns |
|---|---|
| P1 | `src/samvad/protocol.py`, `transport/`, `server.py`, `routing.py`, `security.py`, `clock.py` |
| P2 | `src/samvad/agent.py`, `supervisor.py`, `budget.py`, `sandbox.py`, `llm/`, `prompts/` |
| P3 | `src/samvad/store/` |
| P4 | `src/samvad/events.py`, `dashboard/index.html`, `experiments/`, `scripts/` |

If a fix requires changing another layer, **say so and stop**. Do not "improve" a neighbouring module on the way past. That is the most common way a day is lost here.

**2 — `protocol.py` is frozen.** It matches [docs/PROTOCOL.md](docs/PROTOCOL.md) exactly. Never add, rename, or loosen a field. Never make validation permissive to get a test passing. If the schema seems wrong, report it — a human bumps the version.

**3 — Never invent a message field.** If you need data that the envelope does not carry, it goes in `payload`, or it does not go.

**4 — All development runs under `MOCK_LLM=1`** unless you are explicitly told otherwise. Live API calls cost real money on someone's personal key, and this system spawns recursively.

**5 — Never write an API key into a file.** Keys come from `.env`, which is gitignored. Do not add one to a test, a fixture, a config example, or a docstring.

---

## Invariants — do not break these

These are the design. Code that violates one is wrong even if it passes tests.

- **`POST /message` returns 202 immediately.** It never awaits an LLM call. Replies arrive as new inbound messages.
- **Budget is conserved.** Child slices sum to ≤ the parent's. A child cannot create money or turns. This is what terminates recursion — there is no separate fork-bomb guard.
- **Decrement budget before the API call**, never after. If it will not cover the call, return `budget_exhausted`.
- **`root_task` is immutable** within a `conversation_id`. Copied verbatim, never paraphrased.
- **Order by Lamport clock, never by `timestamp`.** Four devices, four clocks.
- **A duplicate `message_id` returns the cached response** and does not reach the handler.
- **`task_status: complete` requires `exit_code == 0`.** Enforced in a code path, not requested in a prompt. The runtime is the arbiter; the model is advisory.
- **Every message is signed**, on every transport, `inproc` included. No unsigned fast path.
- **Artifacts travel as `sha256:` refs**, not inline bytes.
- **Concurrency cap of 8** in-flight LLM calls per node. Rate limits arrive well before CPU limits.
- **`agent.handle()` returns messages; it never sends them.** Transport belongs to P1.

---

## Style

- Python 3.11+, `async` throughout, Pydantic v2 for anything crossing the wire.
- Type hints on every public function.
- Files under 500 lines. A file that grows past that is doing too much.
- Validate at boundaries: inbound messages, subprocess output, LLM responses.
- Errors that peers should reason about are **protocol states** (`budget_exhausted`, `spawn_refused`, `uncertain`), not exceptions.
- No `shell=True`. Ever. Executors run model-generated code.

---

## Testing

- Write the contract test at a seam **before** the implementation. It is the only thing that catches a confident, incompatible reading of an interface.
- Every layer ships a fake of its neighbours so nobody is blocked: `InProcTransport`, `FakeInbox`, `MockLLM`, dict-backed blob store, synthetic log generator.
- Do not test against a live API. Ever.

```bash
pytest tests/contract/    # seams between layers — these must never be skipped
pytest tests/            # everything
```

---

## Commands

```bash
MOCK_LLM=1 python -m samvad.node --config config/peers.yaml --as agent_a
python scripts/netcheck.py --peer 192.168.1.42:8000      # LAN reachability (no config needed)
python scripts/netcheck.py --peers config/peers.yaml     # ...or from the peer table
python -m experiments.transport                          # measurement 1
start dashboard/index.html                               # renders on synthetic data
```

---

## Git

- Branch per owner: `p1/...`, `p2/...`, `p3/...`, `p4/...`
- Small PRs. **Nobody merges their own.**
- Never commit `.env`, keys, `*.db`, or blob store contents.
- Do not add a `Co-Authored-By` trailer.

---

## When you are unsure

Say so and stop. A wrong assumption that compiles is more expensive here than a question, because three other people are building against the same contract and will inherit it.

If a task needs a change to the protocol, another layer, or an invariant above: **report it, do not do it.**
