# Demo Runbook

Four laptops is four times the failure surface. Everything below exists because something will go wrong.

---

## The night before

- [ ] **Record a full fallback run.** Screen capture, all four terminals plus the dashboard, a complete task end to end. If the network dies tomorrow, this is the demo. Do not skip it — it is the single highest-value item on this list.
- [ ] Every laptop charged, chargers packed
- [ ] Sleep and screen-lock **disabled** on all four (a sleeping wifi adapter drops a peer mid-run)
- [ ] `config/peers.yaml` updated for the demo network's IPs
- [ ] `SAMVAD_SECRET` identical on all four — verify, do not assume
- [ ] System clocks synced (skew beyond ±120 s makes every message fail signature verification)
- [ ] Live API keys funded; circuit breaker set to something you are willing to lose
- [ ] `MOCK_LLM=1` full run passes on the demo network
- [ ] Phone hotspot tested as backup, with its IPs in `config/peers.hotspot.yaml`
- [ ] Everyone knows their speaking part

---

## Thirty minutes before

```bash
python scripts/netcheck.py --peers config/peers.yaml    # all pairs must pass
```

- [ ] All four nodes start clean; `GET /health` green on each
- [ ] Dashboard reachable from the presenting laptop
- [ ] One `MOCK_LLM=1` task completes end to end
- [ ] Terminals sized and readable from the back of the room — large font, dark background
- [ ] Budget reset, cost counter at zero

**If `netcheck` fails, switch to the hotspot now.** Not in five minutes, not after one more try. You have a config file ready for exactly this.

---

## The demo

Roughly twelve minutes. Every beat shows something an in-process system structurally cannot do.

### 1 · The claim (1 min)

Four laptops, four agents, four *different* models. Show `GET /health` on each. Nothing is shared: no process, no memory, no clock.

> "Most multi-agent demos are objects calling methods in one process. Every message you are about to see crosses a real network."

### 2 · One task, end to end (3 min)

Submit a coding task to the planner. On the dashboard: messages flowing between devices, live token cost climbing, per-node context usage.

Point at the **Lamport clocks** ordering the log correctly while the four wall clocks disagree.

### 3 · Fan-out and spawning (2 min)

The planner splits the work; two executors run in parallel on separate devices. A reviewer spawns three verifier children with different lenses and takes a majority vote.

Show the budget: the parent's slice divided among children, summing to the parent's total.

> "A branch that cannot afford one API call cannot spawn. Recursion terminates by running out of money, not by hitting a counter."

### 4 · Remote spawn (1 min)

A peer near its context limit places a worker on an idle peer. The child belongs to A, runs on D, bills A's budget.

> "Context stops being four isolated windows and becomes a shared, redistributable resource."

### 5 · Kill a peer (2 min) — *the money shot*

Mid-fan-out, close an executor's terminal. Do it visibly.

Orphans reparent to the grandparent. In-flight results still arrive. The task completes.

> "This is the demo you cannot give with agents in one process. There is nothing to kill."

### 6 · Partition and heal (2 min)

Drop one laptop off the network. Show divergence. Reconnect. Show reconciliation via the append-only log.

### 7 · The numbers (1 min)

The transport comparison table: in-process against loopback against LAN. Latency, retries, **messages lost**.

> "In-process, delivery cannot fail. On a real network it does — which is why idempotency and logical clocks are in the protocol at all."

---

## When it breaks

| Symptom | Do this | Say this |
|---|---|---|
| A peer unreachable | `--nodes 3`, restart | "Node count is config — it degrades." Then demo the kill anyway; it is the same behaviour |
| Network down entirely | Hotspot config, restart | Keep talking through the architecture while it comes up |
| Hotspot fails too | Loopback on one laptop | "Same envelope, same signing, different transport — which is measurement one" |
| Everything is down | Play the recording | Narrate over it. You measured this system; you can talk about it |
| An agent loops | Show `turns_remaining` hit zero → `abandoned` | "That is the livelock guard doing its job" |
| Cost spikes | Show the circuit breaker trip → `budget_exhausted` | "Refusal is a protocol state, not a crash" |
| An LLM returns nonsense | Show `exit_code != 0` blocking `complete` | "The runtime is the arbiter. The model does not get to grade itself" |

**A failure you can explain is better than a demo that never wobbles.** Every row above is a designed behaviour. If one fires live, you are demonstrating the system working, not failing — say so, plainly, and move on.

---

## Questions you will get

**"Why not just run them in one process?"**
Measurement 1. In-process, delivery cannot fail — so you never build idempotency, retries, or logical clocks, and you never learn why they exist. Here is the table.

**"Isn't this just microservices?"**
The transport is. The interesting parts are not: budget conserved across a spawn tree, context redistributed between machines, and agents whose failure mode is confident wrongness rather than a stack trace.

**"How do you stop them hallucinating?"**
We do not stop it — we stop it *propagating*. The runtime decides completion, not the model. Different models on different devices give uncorrelated errors. Agents can say `uncertain`. And measurement 5 shows two instances of one model approving each other's mistakes.

**"What if an agent lies about its budget?"**
It can. HMAC proves *who* sent a message, not that its contents are true. Byzantine tolerance is future scope, and we say so.

**"Why four agents?"**
Routing, partition, partial failure and majority vote do not exist at N=2. Three is the minimum; four is one per team member.

---

## After

- [ ] Save every agent's SQLite log and the dashboard capture
- [ ] Record what broke and what was asked — it goes in the reflection section
- [ ] **Rotate the API keys** if they were on screen at any point
