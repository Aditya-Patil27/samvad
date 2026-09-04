# Work Distribution

Four people. Four layers. One shared contract.

**The rule that makes this work: split by layer, never by agent.** Every agent in Samvad runs the *same binary* with a different config and system prompt. There is no "Agent A codebase." If two people are both writing an agent, the split is wrong.

**One person owns each file.** No shared ownership, no cross-module edits. If you need something changed in someone else's layer, you ask them — you do not edit it, and neither does your AI assistant.

---

## The four parts at a glance

| | Owner | Layer | Mission | Risk |
|---|---|---|---|---|
| **P1** | | Protocol & Transport | The wire. Envelope, signing, delivery, routing. | **Critical path** — everyone waits on this |
| **P2** | | Agent Core & Supervision | The mind. Prompts, LLM calls, spawning, budget, sandbox. | Highest complexity |
| **P3** | | Memory & Context | The state. Blobs, log, replay, context accounting. | Lowest coupling — safest to start |
| **P4** | | Observability & Experiments | The evidence. Dashboard, measurements, report. | Blocked longest — needs synthetic data early |

Fill in the names before you start. Assign P1 to whoever is most reliable, not whoever is most eager — everything else is downstream.

---

## Dependency graph

```
        ┌────────────────────────────────┐
        │  protocol.py (written jointly)  │
        │  FROZEN end of Day 1            │
        └───────────────┬────────────────┘
                        │
        ┌───────────────┼───────────────┬──────────────┐
        ▼               ▼               ▼              ▼
   P1 transport    P2 agent core   P3 blob store   P4 harness
   P1 server            │               │              │
        │               │               │              │
        └──────────────►│◄──────────────┘              │
                        │                              │
                   P3 message log ─────────────────────┘
```

**Critical path:** `protocol.py` → P1 server → P2 agent → integration. P1 should build the server *before* routing and mDNS; the fancy parts can wait, the inbox cannot.

---

## P1 — Protocol & Transport

> *The wire. If a message moves, P1 owns it.*

### Owns exclusively

```
src/samvad/protocol.py          # THE frozen contract — see docs/PROTOCOL.md
src/samvad/transport/base.py
src/samvad/transport/inproc.py
src/samvad/transport/loopback.py
src/samvad/transport/lan.py
src/samvad/server.py            # FastAPI app, /message, /health, /peers
src/samvad/routing.py           # longest-prefix address resolution
src/samvad/security.py          # HMAC sign/verify, replay window
src/samvad/clock.py             # Lamport
config/peers.example.yaml
```

### Must expose

```python
# protocol.py
class Message(BaseModel): ...              # the envelope, frozen
class Budget(BaseModel): ...
class ArtifactRef(BaseModel): ...

# security.py
def sign(msg: Message, secret: str) -> str
def verify(msg: Message, secret: str) -> bool     # includes replay window check

# clock.py
class LamportClock:
    def tick(self) -> int                  # on send
    def observe(self, remote: int) -> int   # on receive: max(local, remote) + 1

# transport/base.py
class Transport(Protocol):
    async def send(self, msg: Message) -> None    # fire-and-forget, returns immediately

# server.py
def make_app(inbox: Callable[[Message], Awaitable[None]]) -> FastAPI
#   POST /message  -> 202 {"accepted": message_id}   (never blocks on an LLM call)
#   GET  /health   -> 200 {"agent": ..., "lamport": ..., "children": N}
#   GET  /peers    -> the peer table as this node sees it

# routing.py
def resolve(address: str, table: PeerTable) -> Peer   # longest-prefix match
```

### Consumes from others

Nothing. P1 is the root. This is why P1 is the critical path and why P1 does not get to be late.

### Must provide as a mock (week 1)

`InProcTransport` and a `FakeInbox` that records messages, so P2, P3 and P4 can all test without a network. **Ship these before you ship the LAN transport.** Your teammates being unblocked matters more than your layer being finished.

### Definition of done

- [ ] Two nodes exchange a signed message over LAN; both logs show matching `message_id` and correctly ordered Lamport clocks
- [ ] A duplicate `message_id` returns the cached response and does **not** trigger a second handler call
- [ ] A message with a bad signature is rejected with 401 and logged
- [ ] A message older than the replay window is rejected
- [ ] `resolve("agent_b/worker_2/checker_1")` routes to peer `agent_b`
- [ ] `POST /message` returns 202 in under 50 ms while a 30-second LLM call is in flight

### Does not touch

Prompts, model selection, blob storage, the dashboard.

---

## P2 — Agent Core & Supervision

> *The mind. Everything between "a message arrived" and "here is a reply."*

### Owns exclusively

```
src/samvad/agent.py             # handle() — the core loop
src/samvad/supervisor.py        # child lifecycle, reparenting, concurrency cap
src/samvad/budget.py            # conservation, slicing, circuit breaker
src/samvad/sandbox.py           # subprocess execution of model-generated code
src/samvad/llm/base.py
src/samvad/llm/claude.py
src/samvad/llm/ollama.py
src/samvad/llm/mock.py          # MOCK_LLM=1
src/samvad/prompts/*.md         # planner, executor, reviewer, verifier
```

### Must expose

```python
class Agent:
    async def handle(self, msg: Message) -> list[Message]
    # returns zero or more outbound messages; NEVER sends them itself

class Supervisor:
    async def spawn(self, spec: ChildSpec, slice: Budget) -> str   # -> agent path
    async def on_child_result(self, msg: Message) -> None
    async def reparent(self, orphans: list[str], to: str) -> None

class LLMBackend(Protocol):
    async def complete(self, prompt: Prompt) -> Completion
    # Completion carries usage: input, output, cache_read tokens + model id

def run_sandboxed(code: str, timeout: float = 5.0) -> ExecResult
    # tempdir cwd, no shell=True, no network
```

`handle()` returning messages rather than sending them is deliberate — it makes the agent core pure and testable, and it keeps transport entirely in P1's hands.

### Consumes from others

- `protocol.Message` — P1
- `BlobStore.put` / `.get` — P3
- `ContextTracker.used()` — P3

### Builds against (week 1–2)

A dict-backed fake blob store and `MockLLM`. Do not wait for P3.

### Definition of done

- [ ] `handle()` on a `task_request` returns a well-formed `task_result` under `MOCK_LLM=1`, no network
- [ ] A parent spawning 4 children slices its budget so the four slices sum to exactly the parent's
- [ ] A branch whose slice cannot afford one call returns `spawn_refused`, not a crash
- [ ] `max_depth` is enforced — a depth-4 spawn under `max_depth: 3` is refused
- [ ] Concurrency semaphore caps in-flight LLM calls at 8 per node
- [ ] 429 responses retry with exponential backoff
- [ ] Killing a parent mid-fan-out reparents orphans; their results still arrive
- [ ] `run_sandboxed` kills an infinite loop at the timeout and returns a non-zero exit code
- [ ] A reviewer cannot set `task_status: complete` when `exit_code != 0` — enforced in code, not in the prompt

That last one is the anti-hallucination guarantee. It is a code path, not a request to the model.

### Does not touch

The envelope schema, HTTP routes, SQLite, the dashboard.

---

## P3 — Memory & Context

> *The state. Everything that outlives a single message.*

### Owns exclusively

```
src/samvad/store/blobs.py       # content-addressed artifact store
src/samvad/store/log.py         # SQLite append-only message log
src/samvad/store/context.py     # token accounting, backpressure decisions
src/samvad/store/compact.py     # hot/warm/cold tiering
src/samvad/store/schema.sql
```

### Must expose

```python
class BlobStore:
    def put(self, data: bytes, kind: str) -> ArtifactRef   # sha256 + summary + token count
    def get(self, ref: str) -> bytes | None
    def has(self, ref: str) -> bool

class MessageLog:
    def append(self, msg: Message) -> None
    def replay(self) -> Iterator[Message]                  # rebuild state on boot
    def seen(self, message_id: str) -> Message | None      # backs P1's idempotency

class ContextTracker:
    def used(self) -> int                                  # via token counting, not a guess
    def limit(self) -> int
    def should_send_full(self, peer: ContextState) -> bool  # backpressure at 90%
```

Plus one route contributed to P1's app: `GET /blob/{hash}`.

### Seam to watch

Idempotency is split: **P3 stores** (`seen()`), **P1 decides** (return the cached response instead of re-dispatching). Agree on this on day one or you will both build half of it.

### Consumes from others

`protocol.Message` — P1. That is all. **P3 has the lowest coupling of any layer — start here if you want to be productive on day two.**

### Definition of done

- [ ] The same bytes stored twice produce one blob and the same ref
- [ ] `GET /blob/{hash}` returns bytes; an unknown hash returns 404
- [ ] Killing an agent mid-conversation and restarting replays the log and resumes correctly
- [ ] `used()` matches the API's reported input tokens within 5%
- [ ] `should_send_full()` returns `False` when the peer reports above 90%
- [ ] A conversation exceeding the hot-tier limit compacts without losing the `root_task`

### Does not touch

Prompts, HTTP transport, model selection, the dashboard.

---

## P4 — Observability & Experiments

> *The evidence. This layer is what the marks are actually for.*

### Owns exclusively

```
src/samvad/events.py            # SSE /events stream
dashboard/index.html            # live message flow + running cost (one file, no build)
experiments/transport.py        # measurement 1
experiments/topology.py         # measurement 2
experiments/grounding.py        # measurement 3
experiments/context_dedup.py    # measurement 4
experiments/disagreement.py     # measurement 5
experiments/fixtures.py         # synthetic log generator — build this FIRST
scripts/netcheck.py             # DONE - LAN reachability, refused vs timeout
scripts/cache_check.py          # prompt-cache hit verification
docs/RESULTS.md
```

### Consumes from others

P3's SQLite log, and an `/events` SSE stream. **Read-only. P4 never writes to the system.** That zero-coupling is what lets P4 work without blocking anyone.

### The blocking problem, and its fix

P4 has nothing real to observe until week 2. So P4's week 1 is:

1. **Run `scripts/netcheck.py` on all four laptops** — already written and working. Highest-priority task in the entire project (see [Risks](../Samvad.md#risks)); if campus wifi isolates clients, the architecture needs rethinking and everyone needs to know in week 1, not week 4. Read the refused-vs-timeout distinction in its docstring before interpreting a failure — *refused* means the path works and a node simply is not running; only *timeout* is isolation.
2. **`experiments/fixtures.py`** — a synthetic message log generator. `dashboard/index.html` already ships an inline synthetic run; port that scenario to Python so the experiments share it.
3. **CI** — already wired in `.github/workflows/ci.yml` (ruff + contract tests, `MOCK_LLM=1` pinned). Keep it green; with four AI assistants generating code it is the safety net.

### Definition of done

- [ ] `netcheck.py` reports pass/fail for every peer pair, run from any node
- [ ] Dashboard shows live message flow, per-node context usage, and a running USD total
- [ ] Dashboard renders from synthetic data with no node running, and from `/events` when one is
- [ ] All five experiments run from a single command and emit CSV
- [ ] Every plot in the report regenerates from that CSV — no hand-made numbers
- [ ] CI runs contract tests on every PR

### Does not touch

Anything in `src/samvad/` outside `events.py`.

---

## Week 1 is joint work

Do not split until these are done. All four people, together.

| Task | Who | Why it must be joint |
|---|---|---|
| `git init`, branch per owner, protected `main` | All | Nobody works without version control |
| **Write `protocol.py` together, freeze it, tag `v2.0.0`** | All | The single highest-leverage hour of the project |
| Write root `CLAUDE.md` carrying the schema | All | Aligns four AI assistants on one contract |
| Run `netcheck.py` on all four laptops | P4 leads | The architecture depends on the answer |
| `MOCK_LLM` round trip A → B → A | P1 + P2 | Proves the wire before any money is spent |

**Why the schema must be frozen jointly:** four AI assistants each asked to "build the agent server" will confidently design four incompatible envelopes that all look reasonable. Integration, not code volume, is your bottleneck. The frozen schema is the only defence.

---

## Integration checkpoints

Hard dates. Everyone stops and integrates.

| | When | Gate |
|---|---|---|
| **I1** | End of week 1 | Mock round trip between 2 nodes, signed and logged |
| **I2** | End of week 2 | 4 nodes, real LLM, one complete task end to end |
| **I3** | Mid week 3 | Local spawning with budget conservation |
| **I4** | End of week 3 | Remote spawn + blob store + backpressure |
| **I5** | Week 4 | Kill-a-peer and partition demos both pass |

If a checkpoint slips, the fix is to cut scope from the *stretch* list, never to skip the checkpoint.

---

## Rules for working alongside AI assistants

These matter more here than on a normal project.

1. **Never let your assistant edit a file you do not own.** Asked to fix a bug in the agent core, it will cheerfully "improve" the protocol module on the way past. That is how a day disappears.
2. **The root `CLAUDE.md` is the shared context.** Keep it current. It is how all four assistants stay aligned — the same mechanism this project is about.
3. **Nobody merges their own PR.** AI writes plausible-and-wrong faster than any of you write plausible-and-right. Review is now the scarce resource, not typing.
4. **Small PRs.** A 2000-line generated PR will not get a real review, and everyone will pretend it did.
5. **Write the contract test before the implementation** at every seam. It is the only thing that catches an assistant's confident, incompatible interpretation of an interface.

---

## Report ownership

Marks come from the write-up, so it is split too.

| Section | Owner |
|---|---|
| Architecture, protocol design, addressing | P1 |
| Agent design, spawning, hallucination controls | P2 |
| Context pooling, memory, persistence | P3 |
| All five measurements, plots, analysis | P4 |
| Reflection (see [Samvad.md](../Samvad.md#reflection-for-the-report)) | All four, together |

Write the reflection last, together, and be honest in it. Where your human team hit divergent schemas, duplicated work, or lost context — those are the exact three failure modes this protocol exists to solve. Documenting that is worth more than another feature.
