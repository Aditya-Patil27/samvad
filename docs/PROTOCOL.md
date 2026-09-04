# Samvad Protocol v2.0

**Status: FROZEN.** Changing this document requires all four owners to agree, a version bump, and a git tag.

This is the contract. Every agent — peer or child, local or remote — speaks exactly this, over every transport. `src/samvad/protocol.py` is the executable form of this document; if they disagree, this document is wrong and must be fixed, not worked around.

---

## Envelope

```json
{
  "protocol_version": "2.0",
  "message_id": "b3e1f2a0-1234-4abc-9def-0987654321ab",
  "conversation_id": "conv-7c1e9a",
  "reply_to": "a91c0f3d-...",
  "turn": 4,
  "lamport": 17,

  "sender":   "agent_a",
  "receiver": "agent_b/worker_2",
  "performative": "task_request",

  "root_task": "Implement a binary search function",
  "payload": {
    "subtask": "add unit tests for empty and single-element input",
    "constraints": ["O(log n)", "Python 3.11", "pytest"]
  },

  "artifacts": [
    {"ref": "sha256:9f2a...", "kind": "python_source",
     "summary": "binary search impl, 40 lines", "tokens": 380}
  ],
  "claims": [
    {"claim": "bisect_left is O(log n)",
     "evidence": "test_bisect.py::test_complexity passed",
     "evidence_type": "test_output"}
  ],

  "spawn":   {"parent": "agent_a", "depth": 1, "max_depth": 3},
  "budget":  {"usd_remaining": 0.42, "turns_remaining": 8},
  "context": {"used": 14200, "limit": 200000},
  "cost":    {"input": 4200, "output": 830, "cache_read": 3900,
              "usd": 0.0031, "model": "claude-opus-5"},

  "task_status": "in_progress",
  "sig": "hmac-sha256:...",
  "timestamp": "2026-08-24T10:15:00Z"
}
```

---

## Fields

### Identity and ordering

| Field | Type | Required | Rules |
|---|---|---|---|
| `protocol_version` | `str` | yes | Exactly `"2.0"`. A mismatch is rejected with 400, never coerced |
| `message_id` | `uuid4 str` | yes | Unique per message. **The idempotency key** |
| `conversation_id` | `str` | yes | Stable for a whole task tree, children included |
| `reply_to` | `uuid4 str` | no | The `message_id` being answered. Absent on a conversation's first message |
| `turn` | `int ≥ 0` | yes | Increments per exchange within a conversation |
| `lamport` | `int ≥ 0` | yes | Sender's logical clock at send time. See below |
| `timestamp` | ISO 8601 UTC | yes | Wall clock. **For humans and the replay window only — never for ordering** |

**Lamport rules.** On send: `local += 1`, stamp it. On receive: `local = max(local, msg.lamport) + 1`. Order logs by `(lamport, sender)` — never by `timestamp`. Four devices means four clocks; wall time will lie to you and the mismatch will appear in your own demo recording.

### Addressing

| Field | Type | Required | Rules |
|---|---|---|---|
| `sender` | agent path | yes | See paths below |
| `receiver` | agent path | yes | Routed by longest-prefix match |
| `performative` | enum | yes | The speech act. See table below |

**Agent paths** are `/`-separated, lowercase, `[a-z0-9_]` per segment:

```
agent_b                       a peer
agent_b/worker_2              a child of that peer
agent_b/worker_2/checker_1    a grandchild
```

Only peers appear in the peer table. A message to `agent_b/worker_2` routes to `agent_b`, which forwards. Depth in a path must equal `spawn.depth`.

### Task content

| Field | Type | Required | Rules |
|---|---|---|---|
| `root_task` | `str` | yes | **Immutable for the whole conversation.** Copied verbatim, never paraphrased, re-injected into every prompt |
| `payload` | `object` | yes | Free-form per performative. Validated per-performative, not globally |
| `task_status` | enum | yes | Belongs to the *task*, not the envelope |

`root_task` is immutable because by turn six, agents that only see paraphrases are solving a different problem. Goal drift is hallucination's quiet cousin. A receiver that finds `root_task` changed within a `conversation_id` must reject the message.

**`task_status`:** `pending` · `in_progress` · `complete` · `needs_revision` · `uncertain` · `abandoned`

### Artifacts

Never inline a payload that could be a reference. Four agents holding the same bytes is how a 4×200K pool collapses to 200K.

| Field | Type | Rules |
|---|---|---|
| `ref` | `str` | `sha256:` + 64 hex chars |
| `kind` | `str` | `python_source`, `test_output`, `traceback`, `plan`, `text` |
| `summary` | `str` | ≤ 200 chars. What the receiver decides from |
| `tokens` | `int` | Cost of hydrating it, so the receiver can decide before it pays |

The receiver fetches `GET /blob/{hash}` from the sender only if it decides it needs the bytes.

### Claims

Every factual assertion an agent makes carries its evidence.

| `evidence_type` | Meaning | Trusted |
|---|---|---|
| `test_output` | A test actually ran and produced this | Yes |
| `file_content` | Read from a real file or blob | Yes |
| `peer_report` | Another agent said so | Conditionally |
| `model_prior` | The model believes it. Nothing ran | **No — counted as ungrounded** |

Agents label their own claims. The ratio of grounded to `model_prior` claims is [measurement 3](EXPERIMENTS.md#3--grounded-claim-ratio). Making a model declare its own unsupported claims is cheap and works far better than it should.

### Spawn

| Field | Type | Rules |
|---|---|---|
| `spawn.parent` | agent path | Who to report to. Equals `sender` minus the last segment |
| `spawn.depth` | `int ≥ 0` | 0 for peers. Must equal the path depth |
| `spawn.max_depth` | `int` | Inherited unchanged from the parent. A child can never raise it |

### Budget — conserved

**A child can never hold more than its parent gave it.**

| Field | Type | Rules |
|---|---|---|
| `budget.usd_remaining` | `float ≥ 0` | Sliced among children; slices sum to ≤ the parent's |
| `budget.turns_remaining` | `int ≥ 0` | Same conservation rule |

Conservation is why recursion terminates: a branch whose slice cannot afford one API call cannot spawn, and answers `spawn_refused`. There is no separate fork-bomb guard because there does not need to be one.

An agent **decrements before the call, not after.** If the remainder will not cover it, return `budget_exhausted` and do not call.

### Context — advisory

| Field | Type | Rules |
|---|---|---|
| `context.used` | `int` | From token counting, **not** an estimate |
| `context.limit` | `int` | This agent's window |

Backpressure: a sender seeing `used / limit > 0.9` on its peer switches to references and summaries only. Advisory, not enforced — a peer that ignores it just runs out of context, which is its own problem.

### Cost — reporting only

Present on every `task_result` and `child_result`.

| Field | Type |
|---|---|
| `cost.input` / `output` / `cache_read` | `int` tokens |
| `cost.usd` | `float`, computed from the model's published rate |
| `cost.model` | model id, e.g. `claude-opus-5`, `claude-haiku-4-5`, `ollama:qwen2.5-coder` |

`cache_read` being persistently zero means something in the prompt prefix is changing between calls — check for a timestamp in the system prompt. See [SETUP.md](SETUP.md#prompt-caching-check).

### Signature

`sig` is `hmac-sha256:` + hex digest, computed over the canonical JSON of every field **except `sig` itself**, with keys sorted and no whitespace.

Verification rejects: a bad digest (401), a `timestamp` outside ±120 s (401, replay), or an unknown `sender` (403).

---

## Performatives

| Performative | Direction | `payload` must contain | Reply expected |
|---|---|---|---|
| `task_request` | any → any | `subtask`, `constraints` | `task_result` |
| `task_result` | any → requester | `result`, `exit_code` if code ran | none, or a follow-up `task_request` |
| `spawn_request` | peer → peer | `role`, `prompt_id`, `slice` | `spawn_ack` or `spawn_refused` |
| `spawn_ack` | peer → peer | `agent_path` | later, a `child_result` |
| `spawn_refused` | peer → peer | `reason` | none |
| `child_result` | child → parent | `result`, `summary` | none |
| `budget_exhausted` | any → sender | `spent`, `needed` | none |
| `uncertain` | any → sender | `question`, `options` | human decision |

`spawn_refused` reasons: `at_capacity` · `context_full` · `budget_policy` · `max_depth`

**`uncertain` exists so an agent has an alternative to confabulating.** An agent with no way to express doubt has exactly one option, and it will take it.

---

## Task state machine

```
pending ──► in_progress ──┬──► complete
                          ├──► needs_revision ──► in_progress
                          ├──► uncertain ──► (human) ──► in_progress
                          └──► abandoned
```

`abandoned` is reached when `turns_remaining` or `usd_remaining` hits zero. That is the livelock guard: reviewer says `needs_revision`, executor revises, forever — the turn budget is what stops it.

**Hard rule, enforced in code and not in a prompt:** an agent may not set `task_status: complete` on a task whose last `task_result` carried `exit_code != 0`. The runtime is the arbiter; LLM judgment is advisory. This is the single most effective hallucination control in the system, and it works only if it lives in a code path.

---

## Transport binding

The envelope is identical across all three transports. Only delivery differs.

| Transport | Delivery | Signed |
|---|---|---|
| `inproc` | asyncio queue | Yes — same code path, no exceptions |
| `loopback` | HTTP to 127.0.0.1 | Yes |
| `lan` | HTTP to a peer's LAN address | Yes |

Signing on `inproc` is not paranoia — an unsigned fast path would be a second code path, and the whole point of [measurement 1](EXPERIMENTS.md#1--transport-comparison) is that the only variable is the wire.

**Delivery is fire-and-forget.** `POST /message` returns `202 {"accepted": message_id}` immediately; the reply arrives later as a *new inbound message*. An LLM call takes 5–60 s — a synchronous response would leave the caller blocked until it timed out, and would make "async" true only inside a process rather than on the wire.

---

## Idempotency

Delivery is at-least-once. Retries are expected.

On receiving `message_id` already in the log: **return the stored response, do not re-dispatch.** A duplicate that reaches the handler means a duplicate LLM call, a duplicate charge, and two divergent answers to the same question.

Split of responsibility: **P3 stores** (`MessageLog.seen()`), **P1 decides** (returns the cached response). Agree on this seam explicitly — it is the one place two layers meet on the same concern.

---

## Versioning

`protocol_version` is `MAJOR.MINOR`.

- **MINOR** — new optional fields. Old agents ignore them.
- **MAJOR** — anything else: renames, removals, semantic changes, new required fields.

A receiver rejects a mismatched MAJOR with 400. **Do not coerce, do not best-effort parse.** With four AI assistants in the repo, silent schema drift is the most likely way this project fails; a loud rejection is the point.

Bumping this file means: all four owners agree → update `protocol.py` → update root `CLAUDE.md` → `git tag protocol-vX.Y`.
