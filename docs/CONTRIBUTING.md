# Contributing

Four people, four AI assistants, one repo. The rules below exist because that combination fails in specific, predictable ways.

**Code volume is not your constraint. Integration is.** You will generate more code in week one than the project needs in total. What you will not generate is code that talks to itself.

---

## Ownership

**One person owns each file. No exceptions.**

| Owner | Owns |
|---|---|
| P1 | `src/samvad/protocol.py`, `transport/`, `server.py`, `routing.py`, `security.py`, `clock.py` |
| P2 | `src/samvad/agent.py`, `supervisor.py`, `budget.py`, `sandbox.py`, `llm/`, `prompts/` |
| P3 | `src/samvad/store/` |
| P4 | `src/samvad/events.py`, `dashboard/`, `experiments/`, `scripts/` |

Need a change in someone else's layer? **Ask them.** Do not edit it, and do not let your assistant edit it while fixing something adjacent — that is the single most common way a day disappears here.

Shared, requiring all four to agree: `protocol.py` · `docs/PROTOCOL.md` · `CLAUDE.md` · `pyproject.toml` · CI config.

---

## Git

```bash
git switch -c p2/spawn-budget-slicing
```

Branch prefix is your owner ID: `p1/`, `p2/`, `p3/`, `p4/`.

`main` is protected: no direct pushes, CI green, one approving review.

**Nobody merges their own PR.** This is the rule that matters most. AI writes plausible-and-wrong faster than any of you write plausible-and-right, so review — not typing — is now the scarce resource on this team.

### Never commit

`.env` · any API key · `config/peers.yaml` (personal IPs) · `*.db` · blob store contents · `.venv/`

If a key does get committed: **rotate it immediately**, then clean history. Rotate first — the key is public the moment it is pushed, and rewriting history does not un-publish it.

### Commits

```
p2: slice budget across children on spawn

Children now receive usd/turns proportional to fan-out width.
Slices sum to <= parent, so a branch that cannot afford one call
cannot spawn. Closes the fork-bomb path.
```

Prefix with your owner ID. Say *why*, not just *what*. Do not add a `Co-Authored-By` trailer.

---

## Pull requests

**Small.** A 2000-line generated PR will not get a real review, and everyone will pretend it did. If yours is large, split it.

```markdown
## What
One or two sentences.

## Layer
P2 — agent core

## Contract impact
None / touches the seam with P3's BlobStore / requires a protocol MINOR bump

## Invariants
- [ ] Budget conservation holds
- [ ] Nothing outside my layer is modified
- [ ] No live API calls in tests
- [ ] Contract tests pass

## Tested
MOCK_LLM=1, 4 nodes, kill-parent-mid-fanout
```

### Reviewer checklist

Read [CLAUDE.md](../CLAUDE.md) invariants first, then check:

- [ ] Files touched are all in the author's layer
- [ ] No new envelope field, no loosened validation
- [ ] `POST /message` still returns 202 without awaiting an LLM call
- [ ] Budget decremented **before** the call, not after
- [ ] Child slices sum to ≤ parent's
- [ ] Nothing sorts by `timestamp` — ordering is Lamport
- [ ] `task_status: complete` is still gated on `exit_code == 0`, in code
- [ ] No `shell=True`
- [ ] No key, no secret, no personal IP
- [ ] Contract test exists for any changed seam

**Verify claims against the diff, not the description.** An assistant-written PR body describes what the code was *meant* to do. Sometimes it does something else.

---

## Testing

```bash
pytest tests/contract/    # the seams — never skip these
pytest tests/
```

**Write the contract test before the implementation** at every seam. It is the only thing that catches a confident, incompatible reading of an interface — which is exactly the failure mode four independent assistants produce.

Every layer ships fakes of its neighbours so nobody blocks: `InProcTransport`, `FakeInbox`, `MockLLM`, a dict-backed blob store, a synthetic log generator.

**No test may call a live API.** Not once, not "just to check." Tests run in CI, CI runs on every push, and a live call in a test is a bill that scales with your commit rate.

---

## Changing the protocol

`protocol.py` and [PROTOCOL.md](PROTOCOL.md) are frozen. To change them:

1. Raise it with all four owners — not in a PR description, in conversation
2. Agree the version bump: MINOR for a new optional field, MAJOR for anything else
3. Update `PROTOCOL.md` first, then `protocol.py`, then root `CLAUDE.md`
4. `git tag protocol-v2.1`
5. All four pull before doing anything else

**Never** loosen validation to make a test pass. **Never** add a field "temporarily." With four assistants in the repo, silent schema drift is the most likely way this project fails; loud rejection is the point.

---

## Working with your assistant

1. **Point it at [CLAUDE.md](../CLAUDE.md) and [PROTOCOL.md](PROTOCOL.md) before asking for code.** Without them it will invent an envelope, confidently, and it will look reasonable.
2. **Name your layer in the prompt.** "I own P3, the store layer" prevents helpful excursions into P1.
3. **Reject cross-layer edits** even when the change is genuinely an improvement. Route it to the owner.
4. **Ask it to write the contract test first**, then the implementation.
5. **Read the diff.** Not the summary. The diff.

---

## When you are blocked

- **On another layer** — use its fake and keep moving. Never wait.
- **On the protocol** — raise it with all four. Do not work around it locally.
- **On the network** — see [SETUP.md](SETUP.md); switch to loopback and carry on.
- **On cost** — `MOCK_LLM=1`. Every time.

If you are blocked for more than an hour, say so in the group. Four people silently stuck on each other for a day is how a five-week project becomes a four-week project.
