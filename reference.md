# Samvad - Project Ideation & Reference

**Samvad** (meaning "dialogue" or "conversation") is a distributed multi-agent system designed to address the shortcomings of typical "multi-agent" simulations by enforcing true distribution across a network.

## The Core Ideation

Most "multi-agent" systems today just run multiple agents in the same process, calling each other's methods in memory. Samvad skips the simulation and does the hard part: independent agents running as separate processes on independent machines, communicating strictly over an unreliable network with no shared state or global clock.

The thesis of the project is to build this system and **measure the difference** between running the same workload in-process vs. over a real LAN.

## Architecture

- **Tier 1 (Peers):** 4 permanent agents (one per team member device), each backed by a different model (e.g., Claude Opus, Haiku, Ollama local).
- **Tier 2 (Children):** Ephemeral workers spawned dynamically for specific subtasks.
- **Work Placement:** A peer low on context or budget can spawn a child on *another peer's device*.

## Key Technical Decisions & Mechanisms

### 1. Transport: Fire-and-Forget + Callback
Instead of synchronous request/response (which would block and timeout during long LLM inferences), agents send a `POST` request and immediately get a `202 Accepted`. The result is delivered later via a new inbound message. This makes the system genuinely asynchronous.

### 2. Recursion Control via Conserved Quantities
A fork-bomb is prevented because budget (USD) and turns flow *down* the spawn tree. A parent slices its remaining budget among its children. When a branch cannot afford an API call, recursion naturally terminates.

### 3. Context Pooling & Deduplication
To prevent 4 agents with 200K context windows from simply storing 4 copies of the same 200K tokens, Samvad uses a **content-addressed artifact store**. Messages pass SHA-256 references instead of payloads. Agents only hydrate the context they need, allowing the collective pool to exceed any single window.

### 4. Hallucination Laundering Controls
Two AI models agreeing is often meaningless if they share training priors. Controls include:
- **Runtime as Arbiter:** Execution exits codes (0 or 1) determine task completeness, not LLM opinions.
- **Heterogeneous Models:** Each peer uses a different model to avoid correlated errors.
- **Adversarial Review:** Reviewers are prompted to *break* the code, not to confirm it's right.
- **"Uncertain" State:** Agents can return `uncertain` instead of guessing.

### 5. Distributed Systems Fundamentals
- **Lamport Clocks:** Used to order logs correctly across 4 physical devices with out-of-sync wall clocks.
- **Idempotency:** Caching `message_id -> response` to prevent retries (due to LAN blips) from causing duplicate API charges.
- **Sandboxed Execution:** Executor models run code safely inside isolated environments.

## Measurements
The project validates its architecture by measuring:
1. Transport performance (in-proc vs loopback vs LAN).
2. Message complexity by topology (star vs ring vs mesh).
3. Grounded-claim ratio (evidence vs model priors).
4. Context deduplication token savings.
5. Correlated error rates (homogeneous vs heterogeneous model pairs).
