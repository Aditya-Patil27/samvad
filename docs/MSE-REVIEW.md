# MSE Review — 21 Sep 2026

**Read your own section tonight. Be able to run your own commands.** The one thing
that sinks a review is a question landing on someone who cannot answer for the code
with their name on it.

Scope for tomorrow: **one laptop, four peers on localhost, local Ollama models.**
The four-laptop LAN demo in [DEMO.md](DEMO.md) is for the final review, not this one.

Contract: [WORK.md](WORK.md) · [PROTOCOL.md](PROTOCOL.md) · [../CLAUDE.md](../CLAUDE.md)

---

## 1 · Who owns what

The split is **by layer, never by agent.** Every agent in Samvad runs the same
binary with a different config and system prompt — there is no "Agent A codebase".
This is the design decision the panel is most likely to probe, and all four of us
need the same answer to it.

| | Name | Layer | Owns | Speaks to |
|---|---|---|---|---|
| **P1** | **Aditya** | Protocol & Transport | the wire — envelope, signing, Lamport clock, routing, HTTP server | architecture, protocol design, addressing |
| **P2** | **Samarth** | Agent Core & Supervision | the mind — agent loop, spawning, budget, sandbox, LLM backends | spawning, budget conservation, hallucination controls |
| **P3** | **Sakshant** | Memory & Context | the state — blob store, message log, context accounting, compaction | persistence, replay, context pooling |
| **P4** | **Tejas** | Observability & Experiments | the evidence — dashboard, SSE events, the five measurements | results, plots, the dashboard demo |

Swap the names if the layers are wrong — it is one table, and everything else in
this file refers to P1–P4, not to people.

**The rule that keeps four assistants from writing four incompatible systems:**
one person per file, no cross-layer edits. If your fix needs someone else's module,
you ask them. Every file in `src/` carries an `OWNER:` header saying so.

---

## 2 · Code map — the "who implemented what" answer

This is the table for the professor. Every row is a real file with an owner header
and a test that proves it.

### P1 — Aditya · Protocol & Transport

| File | What it does |
|---|---|
| [`protocol.py`](../src/samvad/protocol.py) | the frozen envelope — `Message`, `Budget`, `ArtifactRef`, `Claim`, `Spawn`, `ContextState`. 343 lines, matches [PROTOCOL.md](PROTOCOL.md) field for field |
| [`security.py`](../src/samvad/security.py) | HMAC sign/verify plus replay window. **No unsigned fast path, not even in-process** |
| [`clock.py`](../src/samvad/clock.py) | Lamport — `tick()` on send, `observe()` = `max(local, remote) + 1` on receive |
| [`routing.py`](../src/samvad/routing.py) | longest-prefix address resolution — `agent_b/worker_2/checker_1` routes to peer `agent_b` |
| [`server.py`](../src/samvad/server.py) | FastAPI. `POST /message` → **202 immediately, never awaits an LLM call**; `/health`, `/peers`, `/blob/{hash}`, `/events`, and the dashboard at `/` |
| [`transport/`](../src/samvad/transport/) | four transports behind one interface: `inproc`, `loopback`, `lan`, plus the base protocol |
| [`config.py`](../src/samvad/config.py) | peer table loading |

Proof: `tests/contract/test_protocol.py`, `test_clock.py`, `test_routing.py`,
`test_transport.py`, and `scripts/roundtrip_demo.py` — two real uvicorn servers:
signed round trip, duplicate `message_id` returning the cached 202 with zero extra
handler calls, tampered body 401, `root_task` drift 409.

### P2 — Samarth · Agent Core & Supervision

| File | What it does |
|---|---|
| [`agent.py`](../src/samvad/agent.py) | `handle()` — the core loop. Returns messages, **never sends them**; transport stays in P1's hands |
| [`supervisor.py`](../src/samvad/supervisor.py) | child lifecycle, fan-out tracking, child-result merging, retry, reparenting on peer death |
| [`budget.py`](../src/samvad/budget.py) | conservation — child slices sum to ≤ the parent's. This is what terminates recursion; there is no separate fork-bomb guard |
| [`sandbox.py`](../src/samvad/sandbox.py) | subprocess execution of model-generated code. Tempdir cwd, timeout kill, **no `shell=True`** |
| [`llm/`](../src/samvad/llm/) | backend selection from the peer table — `mock`, `ollama:`, `groq:`, `nvidia:`. Paid models raise rather than quietly spend |

Proof: `tests/contract/test_agent.py`, `test_sandbox.py`, `test_llm_mock.py`,
`test_llm_ollama.py`, `test_llm_hosted.py`, `test_failover.py`.

### P3 — Sakshant · Memory & Context

| File | What it does |
|---|---|
| [`store/blobs.py`](../src/samvad/store/blobs.py) | content-addressed artifact store. Same bytes twice gives one blob and one `sha256:` ref |
| [`store/log.py`](../src/samvad/store/log.py) | SQLite append-only message log. `replay()` rebuilds state on boot; `seen()` backs P1's idempotency |
| [`store/context.py`](../src/samvad/store/context.py) | token accounting from the backend's reported usage, not a guess. Backpressure at 90% |
| [`store/compact.py`](../src/samvad/store/compact.py) | hot/warm/cold tiering that never drops `root_task` |

Proof: `tests/contract/test_store.py`. Live: a restart on the same `--db` logged
`replayed 4 messages; lamport resumes at 41`.

### P4 — Tejas · Observability & Experiments

| File | What it does |
|---|---|
| [`events.py`](../src/samvad/events.py) | SSE `/events` stream — the one route P4 contributes into P1's app |
| [`dashboard/index.html`](../dashboard/index.html) | two views of one stream: **Office** (a desk per agent, envelopes flying between them) and **Audit table**. One file, no build step |
| [`experiments/transport.py`](../experiments/transport.py) | measurement 1 — inproc vs loopback vs LAN |
| [`experiments/grounding.py`](../experiments/grounding.py) | measurement 3 — claim grounding and error laundering |
| [`experiments/context_dedup.py`](../experiments/context_dedup.py) | measurement 4 — inline bytes vs `sha256:` refs |
| [`experiments/fixtures.py`](../experiments/fixtures.py) | synthetic log generator, so the whole stack is testable with no network |
| [`scripts/netcheck.py`](../scripts/netcheck.py) | LAN reachability — distinguishes refused from timeout |

Proof: `tests/contract/test_events.py`, `tests/test_fixtures.py`,
`tests/test_measurements.py`, `tests/test_measurement_transport.py`, and the seven
CSVs in [`results/`](../results/).

### If the panel asks about commit history

Answer straight: **the `integration` branch was merged and run from one machine**,
so `git log` shows one committer for the pre-demo merges. The per-owner branches
(`p1/…`, `p2/…`, `p3/…`, `p4/…`) and the `OWNER:` headers are the ownership record.
Do not claim four committers if the log shows one — the layer split is the real and
defensible answer, and it is the one the design is actually built around.

---

## 3 · Where the project actually stands

**22 of 27 definition-of-done items ticked = 81%.** 317 tests passing, `ruff check
src tests` clean — both gates CI runs are green. Counted in demonstrable behaviours
from [WORK.md](WORK.md), not in modules; the metric was chosen deliberately, because
percent-of-modules rewards writing stubs.

What is **not** done, by owner. Say these plainly if asked; a known gap costs less
than a claim that falls over under one question.

| Owner | Open item |
|---|---|
| P2 | The concurrency cap of 8 is on tracked children, not on in-flight `llm.complete()` calls directly. Not yet the same guarantee |
| P3 | The `GET /blob/{hash}` 404 path is tested at the store level, not over HTTP |
| P3 | `Compactor` is implemented but has no tests exercising it |
| P4 | Measurements 2 (topology) and 5 (disagreement) are still stubs; measurement 1 has no LAN row — it needs a second physical machine |
| P4 | No `docs/RESULTS.md` and no plotting code. The CSVs exist; the figures do not |

**One open protocol question, and it is a good one to raise ourselves:** the `Claim`
schema is `{claim, evidence, evidence_type}` with **no claim id and no citation
field**, so a `peer_report` cannot say *which* claim it is citing.
`experiments/grounding.py` currently infers that edge from `reply_to`. Measurement 3's
headline number rests on that inference. Adding `claims[].id` and `claims[].cites` is
a minor version bump under our own rules — cheap now, expensive in week 4. Raising it
before the panel finds it is worth more than hiding it.

---

## 4 · Demo strategy — tomorrow

Four peers on **one laptop**, ports 8000–8003, from
[`config/peers.yaml`](../config/peers.yaml). `agent_a` planner, `agent_b` executor and
`agent_c` reviewer all on `llama3.2:3b`; `agent_d` mock.

### The VRAM trap — already handled

`llama3.2:3b` (2.8 GB) and `qwen3:8b` (6.0 GB) **cannot both be resident on a 6 GB
card.** Ollama unloads one to load the other, measured at 7–58 s per swap. Mid-demo
that reads as a hang.

So `agent_c` has been moved to `llama3.2:3b` for tomorrow: one resident model, zero
swaps, every response in seconds. The qwen3 line is still in
[`peers.yaml`](../config/peers.yaml), commented out — restore it for measurement 5,
which needs uncorrelated model lineages and can batch by condition to pay one swap
instead of forty.

**What this costs us:** the "different models give uncorrelated errors" point is no
longer visible on screen. Samarth makes it verbally in beat 3 instead, and says
plainly that tomorrow's run is homogeneous because of the card, not because of the
design — the model string is per-peer config and `agent_d` is already a different
backend entirely.

### Thirty minutes before

```bash
MOCK_LLM=1 python -m pytest -q                    # 317 passing — worth showing
```

- [ ] Four terminals, one per node, large font, dark background
- [ ] Start each **with `--db`** — without it the log is in-memory and beat 5 has
      nothing to replay
- [ ] `GET /health` green on all four
- [ ] One full `MOCK_LLM=1` task end to end
- [ ] Dashboard open at `http://127.0.0.1:8000/` — **served by the node.** Confirm the
      corner reads **live**, not *synthetic*. Opened as a file it cannot reach
      `/events`, silently falls back to a scripted run, and labels itself "not
      measured data" on the projector
- [ ] `ollama list` shows the models pulled; run one warm-up prompt

### Starting it — four terminals, in this order

`SAMVAD_SECRET` must be **identical in all four terminals** or every message fails
signature verification. Set it first, in each one:

```bash
export SAMVAD_SECRET=samvad-demo          # same string in all four

# b, c, d first -- then a, so nothing is talking to a port that is not open yet
MOCK_LLM=0 python -m samvad.node --config config/peers.yaml --as agent_b --db b.db
MOCK_LLM=0 python -m samvad.node --config config/peers.yaml --as agent_c --db c.db
MOCK_LLM=0 python -m samvad.node --config config/peers.yaml --as agent_d --db d.db

# agent_a last, and it opens the conversation on startup
MOCK_LLM=0 python -m samvad.node --config config/peers.yaml --as agent_a --db a.db \
    --task "Implement binary search over a sorted list" --to agent_b --delay 4
```

`MOCK_LLM=0` is what turns Ollama on; the default is `1` and runs the mock. Drop the
`0` and the whole run is deterministic and instant, which is the fallback.

**Verified on this machine tonight** (mock, four nodes): all four `/health` green,
signed `POST /message` → 202, `/events` carrying `root_task` and `result`, Lamport
99 → 101 across the pair, both logs identical.

### Rehearsal findings — read these, they change how beats 3–5 are run

**1 · `--task` cannot produce a fan-out — use the driver.** `--task` sends a
`task_request`; children only come from a `spawn_request` carrying
`payload.fan_out`. [`scripts/demo_drive.py`](../scripts/demo_drive.py) sends either,
signed, into a running node:

```bash
export SAMVAD_SECRET=samvad-demo          # the same secret the nodes started with

python scripts/demo_drive.py task  --to agent_b          # beat 2
python scripts/demo_drive.py spawn --to agent_b --fan-out 3   # beat 3, then kill agent_b
```

Verified: the task came back `task_result ... complete`, the spawn produced three
`spawn_ack`s with the budget sliced three ways (`turns_left=11` → `4`), and
`agent_a` reported `children: 3`. Anything other than **202** means the secrets
differ — that is the usual reason a node silently ignores everything.

**2 · Kill each peer once, on freshly started nodes.** Verified: the first kill
reparents correctly —

```
[agent_a] agent_b is down -- reparented 3 orphan(s) to agent_a: agent_b/worker_1, agent_b/worker_2, agent_b/worker_3
```

— but killing the *same* peer a second time in one session produces no second
reparent line. `Node._reparented` is never cleared, so orphans already adopted are
correctly not moved twice. **If you rehearse the kill, restart every node before the
real thing.**

**3 · The kill takes about 25 seconds, not six.** This is the most important number
on this page. [DEMO.md](DEMO.md) says six — that was three probes at two seconds.
Measured tonight on Windows loopback: the peer greyed out at **t = 26.4 s** after
the kill. `PROBE_TIMEOUT` is 5 s and three consecutive failures are needed, so a
probe that times out rather than being refused costs 3 × (5 + 2) ≈ 21 s.

Both the terminal line and the dashboard strip do arrive. Plan the narration for
**half a minute of silence** — explain the three-failure guard while you wait, and
say plainly that one dropped packet on campus wifi must not look like a dead laptop.
If that pause is too long to hold the room, P1 can drop `PROBE_TIMEOUT` in
[`config.py`](../src/samvad/config.py) from `5.0` to `1.0`, which brings it to
roughly 6–9 s — **but that is a code change on the day, so decide it tonight or not
at all.**

**4 · Restart-and-resume works exactly as advertised** — verified twice:

```
[agent_b] replayed 4 messages; lamport resumes at 65
[agent_b] replayed 8 messages; lamport resumes at 85
```

### The run — about 8 minutes

| # | Beat | Who | What it shows |
|---|---|---|---|
| 1 | **The claim** (1 min) | Aditya | `GET /health` on four ports. Four processes, four clocks, nothing shared. *"Most multi-agent demos are objects calling methods in one process. Every message you are about to see is signed and crosses a socket."* |
| 2 | **One task end to end** (2 min) | Tejas drives, Samarth narrates | Office view: envelopes flying desk to desk, cost climbing, per-node context. Point at the **Lamport column ordering the log correctly** while the wall clocks disagree |
| 3 | **Fan-out and budget** (1.5 min) | Samarth | The planner splits the work; executors run in parallel. Show the parent's slice divided among children, summing to the parent's. *"A branch that cannot afford one call cannot spawn. Recursion terminates by running out of money, not by hitting a counter."* |
| 4 | **Kill a peer** (1.5 min) — the money shot | Aditya kills, Samarth narrates | Close `agent_b`'s terminal visibly. **It takes about 25 seconds** (measured, see finding 3) — three consecutive failed probes at a 5 s timeout, because one dropped packet is not a dead node. Fill the pause: it is the guard working, not the demo hanging. Watch for `agent_b is down -- reparented N orphan(s)` |
| 5 | **Restart and resume** (1 min) | Sakshant | Restart the killed node **on the same `--db`**. It prints `replayed N messages; lamport resumes at M`. *"A node that restarts at zero reorders its own history."* Scope it honestly: this is restart-and-resume, not full partition-and-reconcile |
| 6 | **The numbers** (1 min) | Tejas | The measured table below |

### The numbers that are real

From [`results/`](../results/), regenerated from CSV — no hand-typed figures.

- **Transport (measurement 1):** in-process accept median **0.030 ms**, loopback
  **1.94 ms** — a **64× difference**, 0 messages lost on either, 360 messages each.
  The LAN row is **deliberately empty** until the four-laptop run. Say so.
- **Grounding (measurement 3):** **51%** of claims grounded directly, **65%**
  transitively, **18% laundered** — a claim whose only evidence is another agent's
  report. That gap is the whole argument for the runtime deciding completion.
- **Context dedup (measurement 4):** `sha256:` refs cut bytes on the wire from
  **14.5 KB to 12.2 KB** median, with 20 hydrations skipped.

Measurements 3 and 4 currently run on **synthetic logs** — the generator is real and
tested, the agents inside it are not. If asked, say so; it is in the CSV's own
`run_source` column and they can read it.

### When it breaks

| Symptom | Do | Say |
|---|---|---|
| Ollama hangs, swapping models | Switch the config to all-`llama3.2:3b`, restart | "Model selection is config — it degrades" |
| A node will not start | Drop to three nodes, demo the kill anyway | "Node count is config; the behaviour is identical" |
| Inference too slow on the projector | `MOCK_LLM=1` and rerun | "Same envelope, same signing, deterministic replies — this is exactly how CI runs it" |
| Everything is down | `http://127.0.0.1:8000/?demo` | Forces the scripted run. **It labels itself synthetic — do not claim it is live** |
| An agent loops | Show `turns_remaining` hit zero → `abandoned` | "The livelock guard doing its job" |
| An LLM returns nonsense | Show `exit_code != 0` blocking `complete` | "The runtime is the arbiter. The model does not grade itself" |

**A failure you can explain beats a demo that never wobbles.** Every row above is a
designed behaviour. If one fires live, we are demonstrating the system working —
say so plainly and move on.

---

## 5 · Questions, and who takes them

| Question | Who | The answer |
|---|---|---|
| "Why not just run them in one process?" | Tejas | Measurement 1. In-process, delivery cannot fail — so you never build idempotency, retries or logical clocks, and never learn why they exist. Here is the table |
| "Isn't this just microservices?" | Aditya | The transport is. The interesting parts are not: budget conserved across a spawn tree, context redistributed between machines, and agents whose failure mode is confident wrongness rather than a stack trace |
| "How do you stop them hallucinating?" | Samarth | We do not — we stop it *propagating*. `task_status: complete` requires `exit_code == 0`, enforced in a code path, not asked of the model. Different models give uncorrelated errors. Agents can say `uncertain` |
| "What if an agent lies about its budget?" | Aditya | It can. HMAC proves *who* sent a message, not that its contents are true. Byzantine tolerance is future scope and we say so |
| "Why four agents?" | Aditya | Routing, partition, partial failure and majority vote do not exist at N=2. Three is the minimum; four is one per team member |
| "Where is your persistence?" | Sakshant | SQLite append-only log, replayed in `(lamport, sender)` order on boot. That is beat 5 |
| "Why is the LAN row empty?" | Tejas | It needs a second physical machine. It is the first thing in the final-review run, and leaving it blank beats filling it with a loopback number relabelled |

---

## 6 · Tonight

- [ ] **Aditya** — commit the working tree (`dashboard/index.html`, `events.py` and
      `test_events.py` are modified and uncommitted); confirm all four nodes start clean
- [ ] **Samarth** — read [`agent.py`](../src/samvad/agent.py) and
      [`budget.py`](../src/samvad/budget.py); be able to explain budget conservation at
      the whiteboard without slides
- [ ] **Sakshant** — practise the kill-and-restart beat twice. It is the one beat with
      a required flag (`--db`) and a visible failure if it is missed
- [ ] **Tejas** — open the dashboard in both views, know where the Lamport column and
      the cost counter are, and have `results/` open in a second tab
- [ ] **All** — agree who says the first sentence. (The VRAM question in §4 is settled: all local peers on `llama3.2:3b`)

Everyone should be able to answer **"what does your layer do, and how do you know it
works"** in two sentences, ending with the name of a test file.
