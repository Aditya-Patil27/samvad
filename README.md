# Samvad

### Four devices. Four agents. Agents that spawn agents.

A distributed multi-agent system where every agent is a separate process on a separate machine, reachable only over the network. Agents exchange signed JSON messages to work a shared task, and each can spawn ephemeral children — locally, or on a peer's device when it runs out of context.

*Computer Technology course project · Team of 4*

---

## Why

Most "multi-agent" demos run every agent in one process on one machine — objects calling each other's methods in memory. That is multi-agent in name only, and it skips the hard part.

Samvad does the hard part: independent processes, independent machines, an unreliable network, no shared state, no global clock. Then it **measures the difference** — the same workload runs in-process, over loopback, and over real LAN, and we report what changed.

---

## Quickstart

```bash
git clone <repo> && cd samvad
pip install -e ".[dev]"
cp .env.example .env                 # add your API key
cp config/peers.example.yaml config/peers.yaml

python scripts/netcheck.py --peer <teammate-ip>:8000     # do this FIRST
MOCK_LLM=1 python -m samvad.node --as agent_a            # zero-cost run
```

See the dashboard without any of that — it falls back to a synthetic run:

```bash
start dashboard/index.html      # Windows;  `open` on macOS
```

Full instructions: [docs/SETUP.md](docs/SETUP.md)

> **Run `netcheck.py` before you build anything.** Most campus networks block device-to-device traffic (AP/client isolation) — devices reach the internet but cannot reach each other, and the whole architecture dies silently. Find out in week one, not week four.

---

## Documentation

| Doc | What it is | Read it when |
|---|---|---|
| [Samvad.md](Samvad.md) | Full design: architecture, spawning, hallucination controls, context pooling, metering | Start here |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | **The frozen contract.** Every field, every rule | Before writing any code that touches a message |
| [docs/WORK.md](docs/WORK.md) | Four-way ownership, interfaces, checkpoints | Week one, then whenever you are unsure who owns something |
| [docs/SETUP.md](docs/SETUP.md) | Environment, config, running a node, troubleshooting | Setting up |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | Git workflow, review rules | Before your first PR |
| [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md) | The five measurements and how to run them | Week five |
| [docs/DEMO.md](docs/DEMO.md) | Demo-day runbook and recovery | The night before |
| [CLAUDE.md](CLAUDE.md) | Instructions for AI assistants | Automatically — it is loaded every session |

---

## Architecture in one table

| Tier | What | Lifetime | Count |
|---|---|---|---|
| **Peers** | One permanent agent per device, per person, per model backend | Whole session | 4 (config; degrades to 2) |
| **Children** | Ephemeral workers spawned for a subtask | One task | Unbounded, budget-limited |

Every agent runs the same binary. Config and system prompt differ; code does not.

Three things flow **down** the spawn tree and are conserved — money, turns, depth. A branch that cannot afford one API call cannot spawn, so recursion terminates by running out of budget rather than by hitting an arbitrary counter.

---

## Layout

```
src/samvad/
  protocol.py       P1  the frozen envelope
  transport/        P1  inproc · loopback · lan
  server.py         P1  FastAPI inbox — POST /message returns 202
  routing.py        P1  longest-prefix address resolution
  security.py       P1  HMAC sign/verify
  clock.py          P1  Lamport
  agent.py          P2  handle() — the core loop
  supervisor.py     P2  child lifecycle, reparenting
  budget.py         P2  conservation, circuit breaker
  sandbox.py        P2  subprocess execution
  llm/              P2  claude · ollama · mock
  prompts/          P2  planner · executor · reviewer · verifier
  store/            P3  blobs · log · context · compaction
  events.py         P4  SSE stream
dashboard/index.html  P4  live trace, one file, no build
experiments/        P4  the five measurements
scripts/            P4  netcheck (working), cache_check
tests/contract/     all seams between layers — named, skipped until the layers exist
.github/workflows/  CI: ruff + contract tests, MOCK_LLM pinned on
docs/  config/  results/
```

---

## Ownership

Four people, four layers, narrow interfaces. **Not** four agents.

| | Layer | Mission |
|---|---|---|
| P1 | Protocol & Transport | The wire |
| P2 | Agent Core & Supervision | The mind |
| P3 | Memory & Context | The state |
| P4 | Observability & Experiments | The evidence |

**One person owns each file.** Details and interfaces in [docs/WORK.md](docs/WORK.md).

---

## Results

Five measurements, all falling out of instrumentation the design already requires:

1. **Transport comparison** — inproc vs. loopback vs. LAN. *The project's thesis, quantified.*
2. **Message complexity by topology** — star vs. ring vs. mesh, O(N) against O(N²)
3. **Grounded-claim ratio** — claims backed by evidence vs. `model_prior`
4. **Context dedup effect** — tokens per task, with and without artifact references
5. **Correlated error rate** — homogeneous vs. heterogeneous model pairs

Method: [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md) · Results: `docs/RESULTS.md`

---

## Status

**Week 0 — scaffolded, not built.** The tree, packaging, CI and git repo exist. Every module under
`src/samvad/` is a stub that raises `NotImplementedError`, carrying an `OWNER:` header and the
invariants it must not break.

| Working now | Stubbed |
|---|---|
| [`scripts/netcheck.py`](scripts/netcheck.py) — LAN reachability, refused-vs-timeout diagnosis | everything in `src/samvad/` |
| [`dashboard/index.html`](dashboard/index.html) — renders against synthetic data | the five experiments |
| packaging, `.gitignore`, CI, `config/peers.example.yaml`, `store/schema.sql`, agent prompts | `scripts/cache_check.py` |

`protocol.py` has the enums and a field-by-field TODO. **It is deliberately unwritten** — all four
of you write it together on day one, then tag it `protocol-v2.0`. The contract tests in
`tests/contract/` are named and skipped; unskip them as you build.

No first commit yet. Make it together, after `protocol.py` exists.
