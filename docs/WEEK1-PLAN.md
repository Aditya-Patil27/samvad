# Week 1 Plan — zero to checkpoint I1

**Target: 30–40% by end of week 1.**

Four owners, but delivery risk is not evenly spread. This plan is built so that **one person's output
is the floor and everyone else's is upside** — never a dependency. If all four layers land, week 1
closes near 56%. If only the critical path lands, it still closes above 30%.

Contract: [WORK.md](WORK.md) · [PROTOCOL.md](PROTOCOL.md) · [../CLAUDE.md](../CLAUDE.md)

---

## How we count a percent

Percent-of-modules is a bad metric — it rewards writing stubs. Count the **definition-of-done
checkboxes in [WORK.md](WORK.md)**: each is a behaviour you can demonstrate, and each maps to a
contract test.

| Owner | Layer | DoD items |
|---|---|---|
| P1 | Protocol & Transport | 6 |
| P2 | Agent Core & Supervision | 9 |
| P3 | Memory & Context | 6 |
| P4 | Observability & Experiments | 6 |
| | **Total** | **27** — **13** ticked = **48%** |

- ~~Floor 9/27 = 33%~~ · ~~Target 15/27 = 56%~~
- **Now at 13/27 = 48%.** Target cleared. P1 complete bar a second physical
  machine, P2 complete bar supervision, P3 not started, P4 partial.

Repo state: 252 tests passing, 11 skipped (all P3's store), ruff clean,
0 `NotImplementedError` left in P1's or P2's core path.

**Proven end to end by `scripts/roundtrip_demo.py`** — two uvicorn servers over
real HTTP, not mocks: signed round trip with Lamport ordering, duplicate
`message_id` returning the cached 202 with zero extra handler calls, tampered
body 401, `root_task` drift 409, and `POST /message` returning in 5 ms while
its handler sleeps 3 s.

---

## Strategy: own the spine, stub the ribs, hand out the tests

Three moves, in priority order. They are what make the floor a floor.

**1 — Own the critical path yourself.** `protocol.py`, then P1's wire. Every other layer imports
`Message`; nothing else can start until it exists, and nothing can be demoed without transport.
Whoever carries the project should carry this, because it is the only work that can block everyone
else.

**2 — Build the fakes as insurance, not courtesy.** [WORK.md](WORK.md) frames `InProcTransport`,
`FakeInbox`, the dict blob store and `MockLLM` as favours that unblock teammates. Re-read them as
**fallbacks**: if P3's blob store never arrives, the dict version ships in the demo; if P2's agent
never arrives, `MockLLM` plus a trivial `handle()` still completes a round trip. That is ~50 lines
that guarantee there is something to show at I1 regardless of who delivers.

**3 — Write the contract tests for all four layers up front.** This is the delegation device. A
teammate's task stops being "implement the blob store" — vague, unverifiable, easy to get subtly
wrong — and becomes **"make `tests/contract/test_store.py` pass."** It is the smallest possible ask,
it is unambiguous, and you can check it in one command instead of reading a diff. Writing all 27
tests costs about a day and is the single highest-leverage day in the week.

---

## The week

### Day 1 — freeze the contract

- [ ] Commit the scaffold. Create `main`, protect it, cut `p1/` `p2/` `p3/` `p4/`.
      There are currently **zero commits** — no branch, no PR, no review until this is done.
- [x] ~~`uv sync --extra dev`~~ — done on this machine; **still needed on the other three.**
      `pytest`, `pytest-asyncio` and `ruff` were all missing, so both CI gates were passing locally
      by not running.
- [ ] Transcribe `protocol.py` from [PROTOCOL.md](PROTOCOL.md): `Message`, `Budget`, `ArtifactRef`,
      `Claim`, `Spawn`, `ContextState`, `Cost`. The spec is complete field-by-field —
      **this is typing, not designing.** Half a day.
- [ ] Write the ten bodies in `tests/contract/test_protocol.py`, delete the skip.
- [ ] `git tag protocol-v2.0`.
- [ ] **Run `netcheck.py` across all machines.** Highest-priority task in the project. If campus
      wifi isolates clients the architecture changes, and that must surface in week 1.

> Do this session with whoever shows up — but **do not wait for full attendance**. A frozen envelope
> written by two people on Monday beats a perfect one written by four on Thursday, and the version
> number exists so v2.1 is cheap.

### Day 2 — the tests everyone else works against — **DONE**

- [x] 75 tests written across five contract files. They **skip themselves** while the module they
      target is still a stub and activate the moment it is implemented — no marker to remember to
      delete. Verified by injecting a `Message` before collection: the suite woke up and *failed*,
      which is the point. Vacuous green was the risk.
- [x] Fallbacks built in `tests/fakes.py` — `DictBlobStore`, `RecordingInbox`, `StubLLM`. Kept in
      `tests/` so they sit in nobody's layer, and tested themselves in `tests/test_fakes.py`.
- [ ] **Push, then message each owner one line: "your job is to make `<file>` pass."**
      P1 → `test_transport.py` · P2 → `test_agent.py` · P3 → `test_store.py`

### Day 3 — signing and ordering

- [ ] `security.py` — HMAC sign / verify + replay window
- [x] `clock.py` — `tick()`, `observe()` = `max(local, remote) + 1`. 7 tests green.
- [ ] `transport/base.py` + `inproc.py` + `loopback.py`

### Day 4 — the server

- [ ] `server.make_app()` — `POST /message` → **202, never awaits the handler**
- [ ] `GET /health`, `GET /peers`
- [x] `routing.resolve()` longest-prefix. 8 tests green. **First DoD item ticked.**

### Day 5 — integrate. Checkpoint I1.

`MOCK_LLM=1` throughout, no money spent.

- [ ] Round trip `agent_a → agent_b → agent_a` over `inproc`, then `loopback`, then **LAN**
- [ ] Signed both directions — **no unsigned fast path**, not even in-process
- [ ] Both logs show the same `message_id` and correctly ordered Lamport clocks
- [ ] A duplicate `message_id` returns the cached response; assert the handler ran **once**
- [ ] A tampered body returns 401 and is logged
- [ ] `POST /message` returns 202 in under 50 ms while a slow handler is in flight
- [ ] Tag it. Write down what slipped and why — that is report material, not overhead.

---

## What ticks by Friday

**Floor — 8 items, 30%. Yours, does not depend on anyone.**

| Owner | Definition-of-done item |
|---|---|
| P4 | `netcheck.py` reports pass/fail for every peer pair, from any node |
| P4 | CI runs contract tests on every PR |
| P1 | A message with a bad signature is rejected with 401 and logged |
| P1 | A message older than the replay window is rejected |
| P1 | `resolve("agent_b/worker_2/checker_1")` routes to peer `agent_b` |
| P1 | `POST /message` returns 202 in under 50 ms while a slow call is in flight |
| P1 | Two nodes exchange a signed message; matching `message_id`, ordered Lamport clocks |
| P1 | A duplicate `message_id` returns the cached response, no second handler call |

**Upside — 7 more, to 56%, if the other layers land against the tests you wrote.**

| Owner | Definition-of-done item |
|---|---|
| P2 | `handle()` returns a well-formed `task_result` under `MOCK_LLM=1`, no network |
| P2 | Budget slices from a 4-child spawn sum to exactly the parent's |
| P2 | A branch that cannot afford one call returns `spawn_refused`, not a crash |
| P2 | `max_depth` enforced — a depth-4 spawn under `max_depth: 3` is refused |
| P3 | The same bytes stored twice produce one blob and the same ref |
| P3 | `GET /blob/{hash}` returns bytes; an unknown hash returns 404 |
| P3 | `should_send_full()` returns `False` when the peer reports above 90% |

**Deliberately not this week:** reparenting orphans, compaction, 429 backoff, the five experiments,
and `used()` matching real API tokens within 5% — all need a live API or week-3 machinery. Cutting
from the stretch list is the correct response to a slip; skipping the Friday checkpoint is not.

---

## What actually costs you the week

**R1 — Day 1 slips.** `protocol.py` is the only hard dependency in the graph: P1's transport, P2's
agent, P3's log and P4's fixtures all import `Message`. A two-day slip costs roughly 20 percentage
points and it costs *everyone's* two days, not just yours. Box it to four hours and tag it even if a
field feels imperfect.

**R2 — campus wifi isolates clients.** If peers cannot reach each other the architecture changes.
`netcheck.py` is already written and working. **Read the refused-vs-timeout distinction before
interpreting a failure:** *refused* means the path works and a node simply isn't running; only
*timeout* is isolation.

**R3 — a layer silently never arrives.** The failure mode is discovering on Thursday that P2 or P3
has nothing. The contract tests are the early-warning system: a red test on day 3 is a conversation,
a missing module on day 5 is a crisis. Check the suite daily, not at the checkpoint.

**R4 — review becomes the bottleneck, quietly.** Four assistants generate plausible-and-wrong faster
than four people write plausible-and-right. Keep PRs small, and prefer "does the contract test pass"
over reading a 2000-line generated diff you will nod through.

**R5 — the local toolchain drifts from CI.** The venv runs Python 3.14.0 while CI pins 3.11, and the
dev extras aren't installed — so `ruff` and `pytest` currently pass locally by not running. Fix both
on day 1, before the first PR, not after the first red build.
