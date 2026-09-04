# Experiments

Five measurements. **Owner: P4**, but every layer must emit the instrumentation.

A project with five real measurements beats one with fifteen features. None of these require extra machinery — they all fall out of fields the protocol already carries.

## Rules

- Every run writes a CSV to `results/`. **Every plot in the report regenerates from CSV.** No hand-typed numbers, ever.
- Every run records: git SHA, date, network (`campus-wifi` / `hotspot` / `ethernet`), model per peer, node count.
- Report **median and p95**, not just the mean. LLM latency is heavily skewed and a mean hides it.
- Minimum 20 tasks per condition. Below that you are reporting noise.
- Use a fixed task set (`experiments/tasks.json`, ~30 small coding tasks) so runs are comparable.
- Anything you could not run, say so. An honest gap beats a quiet one.

---

## 1 — Transport comparison

**This is the project's thesis, quantified.** Everything else is supporting evidence.

The claim in [Samvad.md](../Samvad.md#problem--motivation) is that in-process multi-agent systems skip the hard part. This measures exactly how much they skip.

```bash
python -m experiments.transport --tasks experiments/tasks.json --repeat 20
```

Identical workload, identical envelope, identical signing. **Only the wire changes.**

| Condition | Setup |
|---|---|
| `inproc` | 4 agents, one process, asyncio queues |
| `loopback` | 4 processes, one laptop, HTTP to 127.0.0.1 |
| `lan` | 4 laptops, HTTP over the network |

Record per message: `lamport`, transport, `send_ts`, `recv_ts`, bytes, delivered/retried/lost. Per task: wall clock, message count, retries, failures.

| Metric | Expect |
|---|---|
| Median per-hop latency | ~µs → ~ms → ~10s of ms |
| p95 / median ratio | Near 1 in-proc; much worse on LAN |
| Retries per 100 messages | 0 in-proc; non-zero on LAN |
| Messages lost | 0 in-proc; **> 0 on LAN** |

**The interesting number is not latency — it is the retry and loss count.** In-process, delivery cannot fail. On LAN it does, which is why idempotency and Lamport clocks exist at all. That is the whole argument, in one table.

Do not skip signing on `inproc` to make it faster. That would make it a different code path and invalidate the comparison.

---

## 2 — Message complexity by topology

```bash
python -m experiments.topology --nodes 4 --shape star,ring,mesh
```

| Shape | Routing | Expected messages |
|---|---|---|
| `star` | All through the planner | O(N) |
| `ring` | Each peer to its successor | O(N) hops, higher latency |
| `mesh` | Any peer to any peer | O(N²) |

Record total messages, hops per task, wall clock, and messages per completed task.

Plot messages against N for N = 2, 3, 4. With four nodes you get three points per shape — enough to show the shape of the curve, not enough to fit it. **Say that in the report** rather than drawing a confident line through three points.

Note where the theory breaks: mesh should be fastest by hop count, but with four laptops it may not be, because a real network adds per-link variance that the O(N²) model does not have.

---

## 3 — Grounded-claim ratio

```bash
python -m experiments.grounding --tasks experiments/tasks.json
```

Every `claims[]` entry carries an `evidence_type`. Count them.

| `evidence_type` | Grounded |
|---|---|
| `test_output` | Yes |
| `file_content` | Yes |
| `peer_report` | Only if the referenced claim is itself grounded — **follow the chain** |
| `model_prior` | No |

```
grounded_ratio = (test_output + file_content) / total_claims
```

Break it down by role (planner / executor / reviewer) and by model. Planners will score worse than executors — planners reason about work that has not happened yet, so they have nothing to cite. That is expected; report it rather than treating it as a failure.

**Follow `peer_report` chains to their root.** A claim citing another agent that cites `model_prior` is ungrounded. That transitive collapse is error laundering made visible, and it is worth a paragraph of its own.

---

## 4 — Context dedup effect

```bash
python -m experiments.context_dedup --tasks experiments/tasks.json
```

| Condition | Behaviour |
|---|---|
| `inline` | Full artifact bytes in every message |
| `refs` | `sha256:` references, hydrated on demand |

Record per task: total input tokens across all agents, total USD, peak `context.used` per agent, blob fetches, bytes on the wire.

| Metric | Expect |
|---|---|
| Total input tokens | Substantially lower with refs |
| Peak context per agent | Lower — the point of the mechanism |
| Tasks completing before context exhaustion | Higher with refs |
| Extra round trips | Higher with refs — **the cost** |

Report the cost honestly. Refs trade latency for context. Show both sides and say which matters more here.

Note how many hydrations were *skipped* — artifacts referenced but never fetched. That is the pool sharing working: bytes that never entered a second context window.

---

## 5 — Correlated error / disagreement rate

The result this whole design is built around. Run it even if you cut something else.

```bash
python -m experiments.disagreement --pairs homo,hetero --tasks experiments/tasks.json
```

| Condition | Executor | Reviewer |
|---|---|---|
| `homo` | Claude | Claude |
| `hetero` | Claude | Ollama local |

Ground truth is the test suite — `exit_code` decides, not either model.

| Metric | Meaning |
|---|---|
| Disagreement rate | How often executor and reviewer differ |
| Reviewer catch rate | Bad code correctly rejected |
| False accept rate | **Bad code approved — this is the number that matters** |
| False reject rate | Good code rejected (the cost of suspicion) |

The hypothesis: **`homo` has a higher false-accept rate than `hetero`.** Two instances of one model share training priors, so their errors correlate, and the reviewer waves through exactly the mistakes it would have made itself.

If that holds, you have demonstrated error laundering empirically on real hardware. If it does not hold, report that too and think about why — small sample, tasks too easy for the failure to appear, or the test suite doing the real work regardless of who reviews. A negative result honestly analysed is worth more than a positive one you had to squint at.

Extension, if time allows: a three-lens panel (correctness / edge cases / execution) versus three identical reviewers, at matched token cost. Diversity against redundancy, controlled for spend.

---

## Secondary, if time allows

**Fan-out speedup.** Wall clock against fan-out width 1, 2, 4, 8. Amdahl's law on real hardware, with merge cost visible. Expect diminishing returns — and expect the crossover, where another worker makes things *slower*, to be the interesting point.

**Cost versus quality.** Pass rate against USD for: single agent · agent + reviewer · agent + 3-lens panel. Does fan-out-and-vote earn its tokens? Plot pass rate on cost and find the knee.

**Model routing saving.** Total USD with all calls on Opus 5, versus judgment calls routed to Haiku 4.5. Report the saving and, crucially, whether quality moved.

---

## Reporting

`docs/RESULTS.md` carries every plot with its CSV path and git SHA.

For each measurement: what you expected, what you got, and what surprised you. **The surprises are the most valuable part of the write-up** — they are the evidence you ran a real system rather than a simulation of one.
