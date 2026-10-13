# Roadmap

Ten phases. Each one introduces a specific set of AI engineering concepts, and each one ends with
a working feature, automated tests, and a measurement or demonstration.

The ordering is deliberate: capabilities are added when a phase demonstrates the problem they
solve, so the repository tells the story of why the architecture evolved the way it did.

| Phase | Feature | Concepts |
|---|---|---|
| 1 | Basic autonomous agent | Agent loops, reasoning, tool calling |
| 1.5 | Live scenario demo | Streaming traces, deployment, cost and abuse control |
| 2 | Memory system | Short-term, episodic, semantic, procedural memory |
| 3 | Context engineering | Token budgets, compaction, lost-in-the-middle |
| 4 | MCP integration | MCP servers, clients, tools, resources |
| 5 | Agent skills | Progressive disclosure, reusable procedures |
| 6 | RAG system | Embeddings, vector search, hybrid retrieval |
| 7 | Multi-agent architecture | Orchestration, delegation, agent boundaries |
| 8 | AI gateway | Model routing, fallback, token accounting |
| 9 | Human approvals | Permissions, idempotency, durable execution |
| 10 | Production readiness | Tracing, evaluation, security, deployment |

---

## Phase 1 — Build an agent from scratch

**Goal:** understand exactly how an autonomous AI agent works, with no framework in the way.

Three tools over simulated business data:

```
lookup_order(order_id)
get_refund_policy()
create_refund_request(order_id, reason)
```

The loop:

```
          Receive Goal
               |
               v
          Call Model
               |
               v
        Model Decides
               |
       +-------+-------+
       |               |
       v               v
   Call Tool       Final Answer
       |
       v
   Validate Tool  <-- schema + permissions, in application code
       |
       v
   Execute Tool
       |
       v
   Observe Result
       |
       +----> Call Model Again
```

Built as five engineering challenges, each with an acceptance test:

| # | Challenge | Acceptance test |
|---|---|---|
| 1 | Tool calling | Agent receives a request and selects the correct tool |
| 2 | Multi-step reasoning | Later tool choices depend on earlier results |
| 3 | Failure recovery | An injected tool failure is observed and recovered from, not ignored |
| 4 | Guardrails | A tool the agent is not authorized to use is blocked at execution |
| 5 | Evaluation | 20 scenarios measure tool selection and final outcome |

**Acceptance criteria**
- Agent autonomously completes the valid-refund path without hardcoded step ordering
- Missing order number produces a clarifying question, never an invented order
- Ineligible request is explained and no refund is created
- Tool schema violations and permission denials are enforced in code and surfaced to the model
- Loop terminates on `end_turn` or a step ceiling — never runs unbounded
- Test suite runs offline against a scripted model; live scenarios run on demand

**Deliverable:** a working agent that autonomously selects tools to investigate support requests,
plus a 20-scenario evaluation suite.

---

## Phase 1.5 — Live scenario demo

**Deployed** — live at [agentops-weld.vercel.app](https://agentops-weld.vercel.app), nine
scenarios, no credential in production. Live mode is the remaining piece. Full plan: [`phase-1.5.md`](phase-1.5.md).

A deployed page where a visitor picks a customer with a specific problem and watches the agent
investigate and resolve it, tool call by tool call, including the calls it refuses. Eight fixed
scenarios drawn from the Phase 1 evaluation dataset, replayed from recorded real runs by default
with an opt-in live mode behind rate limits and a daily token budget. Hosted on Vercel rather than
DigitalOcean (DD-014), since the page has no database and no credential.

Numbered 1.5 because it is an insertion driven by a product decision, not a step in the original
learning sequence. See DD-011 for why it was pulled ahead of memory, and what that costs.

Recording the scenarios exposed a gap in the Phase 1 evaluation itself: 19/19 passed without any
live scenario exercising the code-level permission check, which was covered only offline by a
scripted model. S21 was added to the dataset to close it, and it is the scenario the demo leads
with — the agent attempts a tool it has no scope for, is denied in application code, and says so.

Step 5 built the page: `frontend/` on Vite + React + TypeScript, no state library, with the
rendering logic tested against the real recording and the page itself mounted in jsdom and driven
event by event.

Steps 3 and 4 made the demo work with no credentials at all: one real `gpt-5` run per scenario is
recorded to disk with the git SHA and model it was captured under, and served back over SSE at the
pace it originally happened. `make record-replays` runs at the close of every phase that changes
agent behavior — a stale replay does not break, it keeps working while demonstrating the old agent.

Step 2 built the scenario catalog: a presentation-only manifest joined to the evaluation
dataset's messages and the seed data's order facts, with the id-not-message control enforced in
code. See DD-013 for why the page states an expectation it does not author.

Step 1 built the live event stream the rest of the phase needs: `SupportAgent.run()` takes an
`on_event` subscriber attached to the run's trace, so the loop and the tool executor publish what
they already record as it happens, rather than only when the run ends. See DD-012 for why a
subscriber on the trace instead of a second loop.

**Acceptance criteria** (abridged — see the full plan)
- A visitor with no context understands what happened without reading documentation
- All eight scenarios replay with no credentials configured
- The client cannot cause the server to send a message outside the dataset
- Budget exhaustion degrades to replay with a visible notice, never an error
- The Phase 1 eval suite passes unchanged — this adds a view, not behavior

**Deliverable:** a public page that makes the agent's behavior, and its guardrails, legible to
someone who will never read the code.

---

## Phase 2 — Agent memory

**Goal:** make the agent remember useful information across interactions.

| Memory type | Implementation | Example |
|---|---|---|
| Short-term | Conversation history | The customer's current request |
| Episodic | PostgreSQL event records | A previous refund attempt failed |
| Semantic | Database + vector search | This customer prefers email |
| Procedural | Versioned skill files | How to process damaged orders |

**Acceptance criteria**
- Order number stated once is not asked for again within a conversation
- A brand-new conversation can answer "what happened with my previous refund request?"
- A recorded `refund_attempt_failed` episode changes behavior on the next attempt
- Retrieval is scoped per customer — no cross-customer leakage

**Deliverable:** an agent that maintains useful information across independent sessions.

---

## Phase 3 — Context engineering

**Goal:** measure how context size, ordering, and compression affect agent performance.

Three experiments, each recording accuracy, input tokens, output tokens, latency, and cost:

1. **Lost in the middle** — place a decisive fact at the beginning, middle, and end of a long
   context; measure retrieval rate across trials and models.
2. **Context compression** — compare full context, sliding window, and summarization.
3. **Tool description overload** — 5 vs 20 vs 50 tools; measure correct tool selection.

**Acceptance criteria**
- Every experiment is re-runnable from a single command with a fixed seed set
- Results are committed as data, not prose — including results that show no improvement
- Trial counts are large enough to distinguish signal from noise

**Deliverable:** a reproducible benchmark showing how context decisions affect accuracy, cost,
and latency.

---

## Phase 4 — MCP server

**Goal:** understand how AI applications discover and use external tools.

Expose the existing business functions through an MCP server instead of calling Python directly.

- **Tools:** `lookup_order`, `get_customer`, `check_refund_eligibility`, `create_refund_request`
- **Resources:** refund policy, shipping policy, product warranty information
- **Prompts:** investigate damaged order, investigate missing shipment

**Acceptance criteria**
- A second MCP-compatible client connects to the same server and uses the same tools
- Authorization is tested separately from connectivity: discovering a tool does not grant
  permission to execute it
- The agent's behavior is unchanged by the transport swap, verified by the Phase 1 eval suite

**Deliverable:** a standalone MCP server other compatible AI applications can use.

---

## Phase 5 — Agent skills

**Goal:** understand the difference between giving an agent capabilities and teaching it procedures.

MCP gives the agent access to systems. Skills tell it how to perform a kind of work.

```
skills/
└── damaged-order/
    ├── SKILL.md
    ├── refund_policy.md
    └── response_template.md
```

**Acceptance criteria**
- Ten skills exist; only names and descriptions are loaded up front
- Full instructions load when the agent selects a skill
- Token consumption is measured against loading every skill eagerly
- Financial limits and permissions remain enforced in code, not in skill prose

**Deliverable:** a reusable skill system with dynamic loading and a measured token comparison.

---

## Phase 6 — RAG knowledge base

**Goal:** give the agent company documents without sending every document to the model.

Knowledge base: refund policies, warranty agreements, shipping procedures, product manuals,
internal support documentation, policy exceptions.

Shipped in versions so each increment can be measured:

| Version | What it adds |
|---|---|
| 1 | Ingestion, chunking, embeddings, vector search, answers with citations |
| 2 | Hybrid retrieval — semantic plus keyword |
| 3 | Reranking a larger candidate set before the model sees it |
| 4 | Self-correcting retrieval — detect insufficient evidence, rewrite the query, retry |
| 5 | Knowledge updates — verify outdated policy no longer influences answers |

Optional: a knowledge graph (customer → order → product → warranty policy) compared against
vector search on multi-hop questions.

**Acceptance criteria**
- Retrieval quality is measured per version on a fixed question set
- Every answer carries citations that resolve to real chunks
- Deleting or updating a document demonstrably changes the answer

**Deliverable:** a RAG system with measurable retrieval quality and document freshness.

---

## Phase 7 — Multi-agent orchestration

**Goal:** learn when specialized agents outperform a single agent, and when they do not.

| Agent | Responsibility | Tools |
|---|---|---|
| Intake | Extract a structured customer request | Read-only |
| Research | Retrieve policies and order evidence | Database, RAG |
| Resolution | Recommend actions | Eligibility checks |
| Communication | Draft the customer response | Read-only case data |

A coordinator controls the process. Handoffs are structured and validated, not free-form text.

**The experiment:** run the same 100 scenarios through single-agent and multi-agent architectures,
measuring task completion, policy compliance, tool selection accuracy, latency, token usage, and
cost per task.

**Acceptance criteria**
- Both architectures run the identical scenario set through the identical harness
- Results are reported honestly, including the cases where one agent wins
- Each agent's context and tool grants are scoped to its role

**Deliverable:** a multi-agent system with benchmark results compared against the single agent.

---

## Phase 8 — AI gateway

**Goal:** understand model routing, reliability, and cost management.

Route every request through a central gateway providing model selection by task complexity, token
accounting, per-agent spending limits, rate limiting, retry policy, provider fallback, and prompt
caching where supported.

**The experiment:** compare routing everything to one expensive model against routing simple tasks
to a cheaper one. Measure total cost while tracking whether accuracy changes.

**Acceptance criteria**
- Fallback preserves required capabilities — a model without compatible tool calling is not a
  valid recovery target
- A per-agent budget is enforced, and exceeding it fails closed with a clear error
- Cost per task is attributable to a specific agent run

**Deliverable:** centralized model routing with a cost and performance dashboard.

---

## Phase 9 — Human-in-the-loop approvals

**Goal:** build an agent that can safely perform consequential actions.

Simulated refund rules for this project (example business rules, not universal standards):

| Refund amount | Behavior |
|---|---|
| $0 – $50 | Automatic if eligible |
| $50.01 – $200 | Manager approval |
| Above $200 | Escalate for manual handling |

The agent must pause, persist its state, and resume after an approval decision. A React approval
queue lets an administrator review the proposed action, inspect supporting evidence, approve or
reject, and view execution history.

**The critical exercise:** simulate a payment timeout. The agent must not issue a second refund
without determining whether the first attempt succeeded — idempotency keys, transaction status
checks, bounded retries, and explicit handling of unknown outcomes.

**Acceptance criteria**
- A duplicate-execution test proves no double refund under timeout and retry
- Agent state survives process restart between request and approval
- Every approval decision is attributable to a reviewer and recorded in the audit log

**Deliverable:** a durable approval workflow with safe, simulated financial operations.

---

## Phase 10 — Production readiness

### A. Observability

Every agent execution produces a trace:

```
Trace: agent-run-1024
[00:00] Customer request received
[00:02] Order retrieved
[00:04] Policy retrieved
[00:05] Refund eligibility verified
[00:06] Human approval requested
[00:18] Approval received
[00:19] Refund executed
[00:20] Customer response generated
```

The dashboard shows steps, tool inputs and outputs with sensitive data redacted, model usage,
token costs, latency, errors, approval decisions, and final outcomes.

### B. Evaluation framework

At least 100 scenarios — valid and invalid refunds, missing order numbers, conflicting policies,
outdated documentation, ambiguous requests, payment service failures, and malicious instructions
embedded in documents. Deterministic assertions are kept separate from LLM-based grading, so
security and business rules never depend on another model's judgment.

### C. Security testing

| Attack | Required behavior |
|---|---|
| Prompt injection in a retrieved document | Treated as untrusted content, not instructions |
| Tool execution outside assigned permissions | Rejected by the application |
| Request for another customer's data | Authorization enforced before data is returned |

### D. Deployment

Docker Compose brings up the dashboard, API, agent runtime, MCP server, gateway,
PostgreSQL/pgvector, and observability with a documented setup process.

**Acceptance criteria**
- `docker compose up` yields a working system from a clean clone
- The evaluation suite runs as a gate after prompt, tool, model, or retrieval changes
- Security tests are part of the suite, with deterministic assertions

---

## Out of scope for v1

Deliberately not prerequisites for the platform. Candidates for later, separate exploration:
fine-tuning comparison on a narrow classification task, LoRA, distillation, reinforcement
learning, GPU vs CPU inference benchmarking, quantization tradeoffs.

Learning to build, evaluate, and operate agents is worth more here than weeks spent training
models.

---

## Working agreement

- One branch per phase; a phase is not done until tests and docs land with the code
- A polished vertical slice beats every feature implemented superficially
- Measured results are committed even when an experiment shows no improvement
- Permissions and financial limits live in application code, always
