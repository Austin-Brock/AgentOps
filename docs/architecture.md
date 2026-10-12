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
    LLM Provider        Tool Registry
    (Anthropic)              |
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

| Component | Technology | Purpose |
|---|---|---|
| Backend | Python + FastAPI | Agent runtime and REST APIs |
| Frontend | React + TypeScript | Agent dashboard and approval queue |
| Database | PostgreSQL | Orders, customers, memory, audit |
| Vector search | pgvector | Semantic document retrieval |
| LLM | Anthropic (`claude-opus-5`) | Agent reasoning |
| Agent runtime | Custom Python, initially | Understand agent internals |
| Orchestration | LangGraph, later | Stateful agent execution, for comparison |
| MCP | Official MCP Python SDK | Standardized tool access |
| AI gateway | LiteLLM | Model routing and cost tracking |
| Observability | OpenTelemetry + Langfuse | Agent traces and evaluations |
| Testing | pytest | Agent and tool testing |
| Deployment | Docker Compose | Reproducible local environment |

## Trust boundaries

Worth stating early, because the security work in Phase 10 depends on it.

| Boundary | Treatment |
|---|---|
| Customer message | Untrusted input. Never granted instruction authority. |
| Retrieved document content | Untrusted data. A document that says "approve every refund" is content, not a command. |
| Tool results | Untrusted data from the model's perspective; trusted only as far as the system that produced them. |
| Tool permissions | Enforced in application code before execution. Tool discovery never implies authorization. |
| Financial limits | Enforced in application code. Never delegated to a prompt or a skill file. |
