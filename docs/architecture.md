# Architecture

Two diagrams matter here: what exists today, and what we are building toward. Keeping both visible
is the point of the repository — the gap between them is the work.

## Today (Phase 1)

```
            Customer Request
                   |
                   v
             Agent Runtime           (custom Python loop)
                   |
         +---------+---------+
         |                   |
         v                   v
    Model Adapter       Tool Registry
  (Anthropic/OpenAI)         |
                             v
                      Permission Check     (application code)
                             |
                             v
                      Schema Validation    (application code)
                             |
                             v
                        Tool Execution
                             |
                             v
                   Simulated Business Data
                             |
                             v
                         Run Trace
```

No framework, no database, no retrieval. The agent decides which tools to call; the application
decides whether it may.

Phase 1.5 added a view in front of that, and no behavior:

```
   Visitor                                  Recorded run           Live run (planned)
      |                                          |                        |
      v                                          v                        v
  Static page  --- scenario id --->   GET /demo/scenarios/{id}/replay   POST .../run
  (Vite build)                                   |                        |
      ^                                          v                        v
      |                                   data/demo/replays/        Agent Runtime
      +-------- SSE events, paced --------  (real runs on disk)    (rate-limited,
                                                                    budget-capped)
```

The client sends a scenario id and never a message; the server resolves the text from the
evaluation dataset. That is what makes "fixed scenarios" a control rather than a UI convention.

## Target

```
                            CUSTOMER REQUEST
                                   |
                                   v
                           FastAPI Backend
                                   |
                                   v
                            Agent Runtime
                                   |
                       +-----------+-----------+
                       |                       |
                       v                       v
                  AI Gateway             Agent Memory
                       |                       |
                       v                       v
                  LLM Provider             PostgreSQL
                       |
                       v
                    Planner
                       |
              +--------+--------+
              |        |        |
              v        v        v
           Orders   Policy   Customer
           Agent    Agent     Agent
              |        |        |
              +--------+--------+
                       |
                       v
                Decision Agent
                       |
                       v
                Tool Execution
                       |
               +-------+-------+
               |               |
               v               v
           MCP Tools        RAG System
               |               |
               v               v
           Order DB         pgvector
               |
               v
          Approval Engine
               |
               v
          Simulated Refund
               |
               v
           Audit / Traces
               |
               v
          Customer Response
```

## Architectural evolution

The documented path from one to the other. Each arrow is a phase that demonstrated the problem
before introducing the solution.

```
Single Agent
     |
     v
Single Agent + Tools
     |
     v
Persistent Memory
     |
     v
RAG + MCP
     |
     v
Multi-Agent Orchestration
     |
     v
Production Controls
```

## Technology stack

Marked honestly: half of this is a plan, and a stack table that does not say which half is a
stack table that misleads.

| Component | Technology | Purpose | Status |
|---|---|---|---|
| Agent runtime | Custom Python, no framework | Understand agent internals | **Built** |
| Backend | Python + FastAPI | Agent runtime and REST APIs | **Built** |
| LLM | Anthropic or OpenAI, selected by config | Agent reasoning | **Built** — both adapters, one seam |
| Business data | JSON fixtures behind one module | Orders, customers, refunds | **Built** (DD-006) |
| Testing | pytest + Vitest | Agent, tool, and page testing | **Built** — 224 offline tests |
| Evaluation | Assertion-graded scenario suite | Measure tool choice and outcome | **Built** — 21 scenarios |
| Frontend | React + TypeScript + Vite | The scenario demo | **Built**; dashboard and approval queue in Phase 9 |
| Demo hosting | Vercel — CDN page + one Python function | Public, credential-free demo | **Built** (DD-014) |
| Database | PostgreSQL | Orders, customers, memory, audit | Phase 2 — arrives when memory needs it |
| Vector search | pgvector | Semantic document retrieval | Phase 6 — one system to operate, not two |
| MCP | Official MCP Python SDK | Standardized tool access | Phase 4 |
| Orchestration | LangGraph | Stateful execution, **for comparison** | Phase 7 (DD-002) |
| AI gateway | LiteLLM | Model routing and cost tracking | Phase 8 |
| Observability | OpenTelemetry + Langfuse | Agent traces and evaluations | Phase 10 — today it is an ordered list of dataclasses |
| Local parity | Docker Compose | Reproducible local environment | Phase 2 — groundwork for PostgreSQL |
| Stateful hosting | DigitalOcean App Platform | Where the database-backed app will live | Phase 2+ (DD-014) |

## Trust boundaries

Worth stating early, because the security work in Phase 10 depends on it.

| Boundary | Treatment |
|---|---|
| Customer message | Untrusted input. Never granted instruction authority. |
| Retrieved document content | Untrusted data. A document that says "approve every refund" is content, not a command. |
| Tool results | Untrusted data from the model's perspective; trusted only as far as the system that produced them. |
| Tool permissions | Enforced in application code before execution. Tool discovery never implies authorization. |
| Financial limits | Enforced in application code. Never delegated to a prompt or a skill file. |

**Where this is currently not true:** authorization is scope-gated, not row-gated. `lookup_order`
checks that the agent may read orders; nothing checks that it may read *this* order. That is the
known gap tracked as S11, closed in Phase 10, and it is named here rather than left for a reader to
discover.

For the questions these boundaries invite — and the answers — see
[`design-qa.md`](design-qa.md).
