# Deck content — MSE course project

Slide-by-slide content for the review deck. Paste into the existing template;
this file is content and running order, not design.

**Every number below was measured on this repo and is reproducible.** Provenance
is given per slide. Nothing here is estimated, and nothing is carried over from
the previous deck without being checked against the code.

Run `git rev-parse --short HEAD` before presenting and update the SHA on slide 5.
The numbers here are from `bd4b4c8`, 2026-09-20.

---

## What changes, and why

The previous deck was a **proposal** for a system that is **74% built**
(20 of 27 DoD checkboxes in [WORK.md](WORK.md), 293 tests passing). Every slide
was future tense and not one showed anything running. That is the whole problem:
a deck with no primary evidence reads as generated, because generated is exactly
what it could have been. A reader cannot tell the difference between a plan you
wrote and a plan anyone could have written.

Measured numbers fix that on their own. Nobody can produce
`0.030 ms → 1.938 ms, 775.5 bytes on both` without having built the thing.

Three structural changes:

1. **Two literature slides collapse to one.** 2 of 9 slides on other people's
   work and 0 on your own is the wrong ratio for a project with a working system.
2. **Two new slides: the measurement, and the honest gaps.**
   [EXPERIMENTS.md](EXPERIMENTS.md) already requires the second — "Anything you
   could not run, say so. An honest gap beats a quiet one."
3. **Model claims corrected.** The old deck said Claude Opus 5 and Haiku 4.5;
   `config/peers.yaml` is free-only Ollama plus a mock. Say what you ran.

---

## 1 · Title

Keep as is.

---

## 2 · The claim

> Most multi-agent systems are objects calling methods in one process.
> Every message in this one crosses a real network.

Four permanent peers, one per device, on a LAN. Signed JSON over HTTP. No shared
process, no shared memory, no shared clock.

The point to land: **in-process, delivery cannot fail.** So you never build
idempotency, retries or logical clocks — and never find out why they exist.

---

## 3 · Literature and the gap

*Merges the old slides 3 and 4. Keep the six-theme grouping, drop the decorative
glyphs, and add the four missing author names.*

| Theme | Anchor | ID |
|---|---|---|
| Why MAS fail | Cemri et al. (UC Berkeley), 2025 | 2503.13657 |
| Distributed trust | Zhang et al., 2025 | 2504.07461 |
| Hallucination cascade | Saeid Jamshidi, 2026 | 2606.07941 |
| Correlated errors | Guneet Kohli, 2026 | 2605.29800 |
| Budget-bounded spawning | Qing Ye & Jing Tan, 2026 | 2601.08815 |
| Protocols | Qiaomu Li & Ying Xie, 2025 | 2505.03864 |

**Fix before printing:** the old deck rendered the last ID as `2505.0386` —
four digits, not five. It is a real paper; the ID was truncated. Four of the
twelve entries had no author listed at all, which reads as carelessness even
though every citation checks out. All twelve were verified against arXiv.

**The gap, in one line:**

> Each paper isolates one variable — failure taxonomy, hallucination cascade,
> correlated error, budget accounting, protocol design — inside a simulated,
> centralised or offline setting. None runs one workload three ways to isolate
> what real distribution costs.

That last sentence is what slide 5 answers with a number.

---

## 4 · What we built

*Replaces "Proposed Methodology" and "System Architecture". Present tense.*

One binary. Config and system prompt differ per device; **code does not.**

| Device | Peer | Role | Model |
|---|---|---|---|
| 1 | agent_a | planner | `ollama:llama3.2:3b` |
| 2 | agent_b | executor | `ollama:llama3.2:3b` |
| 3 | agent_c | reviewer | `ollama:qwen3:8b` |
| 4 | agent_d | executor | `mock` |

Source: `config/peers.yaml`. Free-tier only — no API spend on a personal key for
a system that spawns recursively.

Three transports behind one interface — `inproc` (asyncio queues), `loopback`
(HTTP to 127.0.0.1), `lan` (HTTP across devices). Hierarchical addressing
(`agent_b/worker_2`) resolved by longest-prefix match, the same rule as IP
routing.

**Worth saying out loud, because it is a real finding nobody expects:**
on an RTX 3050 (6 GB), `llama3.2:3b` runs at 39 tok/s fully resident in VRAM
while `qwen3:8b` runs at 19 tok/s with 14% of its layers spilled to CPU. The
smaller model is **twice as fast** because it fits. On a 6 GB card, fitting beats
parameter count — and the two cannot be co-resident, so measurement 5 has to
batch by condition or pay a 7–58 s model swap per run.

---

## 5 · Measurement 1 — the thesis, quantified

**This is the slide the old deck was missing.** Build it as the visual centre.

Identical workload, identical envelope, identical signing. Only the wire changes.

| Condition | n | median | p95 | p95/median | retries/100 | lost |
|---|---|---|---|---|---|---|
| `inproc` | 360 | **0.030 ms** | 0.053 ms | 1.75 | 0.00 | 0 |
| `loopback` | 360 | **1.938 ms** | 3.091 ms | 1.59 | 0.00 | 0 |
| `lan` | — | *run at setup* | | | | |

**Headline: 64× median latency, for the same 775.5-byte envelope.**

Supporting points, all in the CSV:

- **Byte-identical on both conditions** (775.5 median). The workload did not
  change; only delivery did. That equality is what makes the comparison valid.
- **One-way delivery inverts.** On `loopback` the receiver's handler is entered
  at 1.34 ms while the sender does not learn of acceptance until 1.94 ms — the
  handler runs *before* the 202 returns. On `inproc` it is the reverse (0.039 ms
  delivery against 0.030 ms accept), because the queue hands back before the
  task is scheduled. Same envelope, different delivery semantics.
- **Zero loss on both — and that is the point.** Loopback does not drop packets.
  The loss and retry columns only become non-zero on real LAN, which is exactly
  the argument: the failure modes the protocol exists to survive do not appear
  until you leave one machine.

Reproduce:

```bash
python -m experiments.transport --conditions inproc,loopback --repeat 20
```

Provenance: `results/transport_summary.csv`, git `bd4b4c8`, 2026-09-20,
720 messages over 120 conversations. No LLM in the loop on any condition — a
model would add 5–60 s per hop and bury a microsecond difference.

**Fill the `lan` row during setup tomorrow**, from the four laptops:

```bash
python -m experiments.transport --conditions lan --peers config/peers.yaml \
    --network campus-wifi --repeat 20
```

If the LAN row is still empty at presentation time, say so and show the two you
have. A missing row you name is evidence of method. A guessed one is not.

---

## 6 · What the runtime enforces

*Replaces the abstract "Proposed Solution" list with the same claims, proven.*

Errors that peers should reason about are **protocol states**, not exceptions:
`budget_exhausted`, `spawn_refused`, `uncertain`.

Live, over real HTTP, from `scripts/roundtrip_demo.py`:

| Invariant | Result |
|---|---|
| `POST /message` never awaits an LLM | **202 in 5 ms** while the handler slept 3.0 s |
| Duplicate `message_id` returns the cached response | handler ran **0** extra times |
| Every message is signed | tampered body → **401** |
| `root_task` is immutable in a conversation | paraphrase → **409** |
| Order by Lamport, not wall clock | 1 → 3 across two independent clocks |

**Budget conservation is the termination proof.** Child slices sum to ≤ the
parent's remaining budget. A branch that cannot afford one call cannot spawn, so
recursion terminates by running out of money — not by hitting a counter. There is
no separate fork-bomb guard because there does not need to be one.

`task_status: complete` requires `exit_code == 0`, enforced in a code path and
not requested in a prompt. **The runtime is the arbiter; the model is advisory.**

---

## 7 · Layers against hallucination

Keep the framing — one agent's invented fact becomes another's accepted premise,
and each layer closes one path for that.

**Trim the list to what is implemented**, and mark the rest as design. Nine
numbered items where some are built and some are not invites exactly the question
you do not want. Built today: runtime as arbiter, `uncertain` as a state,
immutable `root_task`, artifacts by hash, heterogeneous models, self-labelled
grounding (`evidence_type` on every claim).

The measurable one, already run: **measurement 3 counts how often a `peer_report`
claim collapses to `model_prior` when you follow its chain** — error laundering
made visible. Numbers in `results/grounding_summary.csv`.

> **Flag for the team before this slide is final:** measurement 3 as specified
> needs a citation field the frozen envelope does not carry. Claims have
> `evidence_type` but no way to cite the specific upstream claim across a
> message boundary. Raise it with P1 — it is a protocol version bump, not a
> patch, and the deck should not imply the full chain analysis runs end to end
> if it does not.

---

## 8 · Live demo — what you are about to see

*New slide. Sets expectations so a wobble reads as designed, not broken.*

1. Four nodes, four `GET /health`. Nothing shared.
2. One task end to end. Lamport clocks ordering the log while wall clocks disagree.
3. Fan-out: the planner splits work across devices; budget slices sum to the parent's.
4. **Kill a peer.** Its orphans reparent to the grandparent, their in-flight
   results still arrive, and the task completes. Takes ~6 s — three failed probes,
   because one dropped packet is not a dead laptop.
5. **Restart it** on the same log. It replays and resumes its Lamport clock
   rather than restarting at zero.
6. The failure table — a peer refuses, and refusal is a protocol state, not a crash.

Point 4 is the one an in-process system cannot do. There is nothing to kill.

> A failure you can explain is better than a demo that never wobbles.

---

## 9 · What is not done

*New slide. This is a strength, not an apology — and it pre-empts the questions.*

- **Full partition-and-reconcile is not built.** A restart replays its log and
  resumes its clock (beat 6). Two halves diverging and merging is design, not code.
- **Three of five experiments are stubs.** Measurement 1 ran (slide 5);
  3 and 4 have CSVs; topology and disagreement raise `NotImplementedError`.
- **LAN loss and retry numbers are pending** the four-device run.
- **No concurrency cap on in-flight LLM calls.** The supervisor caps *tracked
  children* at 8; nothing caps concurrent `llm.complete()` calls. Not the same
  guarantee, and we do not claim it is.
- **Byzantine tolerance is out of scope.** HMAC proves *who* sent a message, not
  that its contents are true. An agent can lie about its budget. We say so.

22 of 27 DoD checkboxes, 310 tests, 5 weeks.

---

## 10 · Team, roadmap, risk

Keep the old slide 8 largely as is. Two corrections:

- The roadmap said "Wk 2–3: all four layers built in parallel" as future work.
  That is done. Move the marker to week 4.
- Tech stack: drop Claude Opus 5 / Haiku 4.5, which were not what ran. FastAPI,
  Pydantic v2, Ollama local models, SQLite log, content-addressed blob store,
  HMAC-SHA256, sandboxed subprocess, one static HTML dashboard.

---

## Presentation-craft notes

Six problems that made the old deck read as machine-written. All cheap to fix.

1. **Every subtitle had the same rhythm** — "Six recent threads, one
   still-untested combination" / "Five isolated gaps, no combined study" /
   "Build once, run three ways, measure the difference" / "Team, timeline, stack,
   and risk". Six slides, six identical comma-balanced constructions. This is the
   loudest tell. Vary them, or cut them; several slides need no subtitle at all.
2. **Suspiciously round taxonomies** — exactly 5 gaps, exactly 9 layers, exactly
   5 experiments, steps 01–05. Real work has 4 of one thing and 7 of another.
   Where a count is genuine, keep it; where it was padded to reach a round
   number, cut the padding.
3. **The glyph set** ▦ ⌘ ⚠ ⚖ ⏱ ⇄ is arbitrary — ⌘ is the Mac command key,
   used as the icon for "Why Multi-Agent Systems Fail". Drop them all.
4. **"SYNTHESIZED GAP STATEMENT"** — labelling your own synthesis as synthesized.
   Call it "The gap".
5. **Em dash in nearly every bullet.** Vary the punctuation.
6. **Letter-spaced ALL-CAPS headers** are a template default, not a choice.

The deepest fix is none of these. It is slide 5. A deck whose centre is a table
you measured cannot be mistaken for a generated one, because the numbers could
not have come from anywhere else.
