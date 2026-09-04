# Setup

---

## Do this first, before anything else

**Verify your four laptops can reach each other.** Not the internet — each other.

```bash
python scripts/netcheck.py --peer 192.168.1.42:8000     # no config needed
python scripts/netcheck.py --peers config/peers.yaml    # once peers.yaml exists (needs pyyaml)
```

Read its verdict carefully — the distinction is the whole point:

| Result | Means |
|---|---|
| `refused` | **The network path works.** That peer just has no node running. Fine |
| `timeout` | **You cannot reach the device at all.** This is the isolation signature |

**Internet up plus every peer timing out is AP/client isolation.** Most institutional networks run it: every device reaches the internet, no device reaches any other. If that is your campus wifi, Samvad cannot run on it — and you need to know in week one, not week four.

Manual cross-check, from device 1 with device 2 running a node:

```bash
ping 192.168.1.42                          # may pass even when HTTP is blocked
curl -m 3 http://192.168.1.42:8000/health  # this is the real test
```

If `curl` times out but `ping` works, or both fail while the internet works, you are isolated. Options, in order of preference:

1. **Phone hotspot.** Usually does not isolate. Test it — this is likely your demo network.
2. **A dedicated router**, no internet needed.
3. **Ethernet switch**, if the lab has one.
4. **Ask IT** for a non-isolated SSID. Slow, but ask early.

Record which networks pass in `docs/RESULTS.md`. You will need a known-good network on demo day and you do not want to be discovering one that morning.

---

## Prerequisites

- Python 3.11+
- Git
- One API key per person (do not share one — the metering design assumes four independent budgets)
- Optional: [Ollama](https://ollama.com) on at least one device, for the offline peer and for [measurement 5](EXPERIMENTS.md#5--correlated-error--disagreement-rate)

---

## Install

```bash
git clone <repo> && cd samvad
python -m venv .venv

source .venv/bin/activate      # macOS / Linux
.venv\Scripts\activate         # Windows

pip install -e ".[dev]"
```

---

## Environment

```bash
cp .env.example .env
```

```ini
ANTHROPIC_API_KEY=sk-ant-...
SAMVAD_SECRET=<shared HMAC secret — identical on all four devices>
SAMVAD_AGENT=agent_a
MOCK_LLM=1                     # leave this on
SAMVAD_MAX_USD_PER_HOUR=1.00   # circuit breaker
SAMVAD_MAX_CONCURRENCY=8
```

`.env` is gitignored. **Never** put a key in a config file, a test, a fixture, or a docstring.

`SAMVAD_SECRET` must be byte-identical everywhere. Generate once, share over something that is not a public channel:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Peer config

```bash
cp config/peers.example.yaml config/peers.yaml    # gitignored — IPs differ per person
```

```yaml
peers:
  agent_a: {host: 192.168.1.41, port: 8000, role: planner,  model: claude-opus-5}
  agent_b: {host: 192.168.1.42, port: 8000, role: executor, model: claude-opus-5}
  agent_c: {host: 192.168.1.43, port: 8000, role: reviewer, model: "ollama:qwen2.5-coder"}
  agent_d: {host: 192.168.1.44, port: 8000, role: executor, model: claude-haiku-4-5}

defaults:
  max_depth: 3
  budget: {usd: 0.50, turns: 12}
```

Find your IP: `ipconfig` (Windows) · `ifconfig`/`ip addr` (macOS/Linux). It changes when you switch networks — re-check before every session.

**Different model per peer is deliberate.** Heterogeneous backends give uncorrelated errors, which is what makes agreement between agents mean anything.

---

## Run a node

```bash
MOCK_LLM=1 python -m samvad.node --config config/peers.yaml --as agent_a
```

One node per device, each with its own `--as`. Check it is alive:

```bash
curl http://localhost:8000/health
# {"agent": "agent_a", "lamport": 0, "children": 0, "budget_usd": 0.50}
```

### Two nodes on one laptop

For solo development, use different ports and the loopback transport:

```bash
SAMVAD_TRANSPORT=loopback python -m samvad.node --as agent_a --port 8000
SAMVAD_TRANSPORT=loopback python -m samvad.node --as agent_b --port 8001
```

Useful for iterating. **Not** a substitute for LAN testing — the difference between them is [measurement 1](EXPERIMENTS.md#1--transport-comparison), which means it is a result, not an inconvenience.

---

## Going live

Only after a full `MOCK_LLM=1` run works end to end.

```bash
MOCK_LLM=0 SAMVAD_MAX_USD_PER_HOUR=0.25 python -m samvad.node --as agent_a
```

Start with a low ceiling. Raise it when you have watched a full task complete and the cost totals look sane.

**Before your first live spawn**, confirm all four are in place:

- [ ] Budget conservation — child slices sum to ≤ parent's
- [ ] `max_depth` enforced
- [ ] Concurrency semaphore at 8
- [ ] 429 retry with backoff

Recursive spawning against a real key is the one way this project can cost you actual money. The guards are not optional.

---

## Prompt caching check

Caching is a **prefix match** — any byte change anywhere in the prefix invalidates everything after it.

```bash
python scripts/cache_check.py
```

If `cache_read_input_tokens` is zero across repeated calls, something in your prefix is changing. Usual suspects:

- A timestamp or message ID in the system prompt
- Unsorted JSON in a serialised tool definition
- A varying tool list between calls
- A prefix under ~1024 tokens (below the minimum, it silently will not cache)

Order is `tools` → `system` → `messages`. Frozen system prompt and `root_task` go first and never change; timestamps, message IDs and the peer's latest turn go last.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `curl /health` works locally, times out from a peer | AP/client isolation, or a firewall | Hotspot; allow Python through the firewall |
| 401 on every message | `SAMVAD_SECRET` differs between devices | Re-share it; check for a trailing newline |
| 401 only sometimes | Clock skew beyond the ±120 s replay window | Sync system time on all four devices |
| Log order looks wrong | Sorting by `timestamp` | Sort by `(lamport, sender)`. There is no global clock |
| Same work done twice | Idempotency cache missed | `MessageLog.seen()` must be checked *before* dispatch |
| `cache_read` always 0 | A varying prompt prefix | See above |
| Cost climbing with nothing happening | Livelock, or a spawn loop | Check `turns_remaining` is decrementing; run under `MOCK_LLM=1` |
| 429s under fan-out | Concurrency cap missing | Semaphore at 8, backoff on retry |
| Node dies on restart, loses everything | Replay not wired | `MessageLog.replay()` on boot |
