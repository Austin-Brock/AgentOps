# AgentOps

A production-oriented AI agent platform demonstrating autonomous tool execution, multi-agent
orchestration, memory, RAG, MCP, human-in-the-loop approvals, and observability.

AgentOps is built as an **AI agent engineering laboratory with a working product at its center**:
an AI-powered customer support platform for a fictional e-commerce company. The product is the
test environment; the experiments and benchmarks are the point.

Each capability is introduced only when it solves a demonstrated problem, and every phase ships
with tests, measurements, and a written design rationale.

## The problem it solves

A customer writes:

> My order #4471 arrived damaged. I'd like a refund.

Instead of generating a sympathetic paragraph, the system:

1. Identifies the order and retrieves it from the database
2. Searches company refund policy
3. Determines eligibility
4. Decides whether more information is needed
5. Requests human approval when the amount requires it
6. Executes a simulated refund with idempotency guarantees
7. Generates the customer response
8. Records the full trace for audit

**The agent decides which tools to use and what to do next.** Permissions, financial limits, and
other safety requirements are enforced in application code — never left to the model.

## Status

| | |
|---|---|
| Current phase | **Phase 1.5 — live scenario demo**, steps 1-2 of 8 complete |
| Agent runtime | Custom Python loop, 3 business tools + 1 deliberately unauthorized |
| Model providers | Anthropic and OpenAI, selected by `AGENTOPS_PROVIDER` |
| Offline tests | 159 passing, no API key required |
| Live scenarios | **19/19 gated** on three consecutive runs against `gpt-5`, 1 known gap |

See [`docs/roadmap.md`](docs/roadmap.md) for all 10 phases and their acceptance gates, and
[`docs/phase-1.md`](docs/phase-1.md) for what Phase 1 actually built.

## Quickstart

```bash
make install     # uv venv + dependencies
make demo        # watch the agent work, no credentials needed
make test        # 159 offline tests; no credentials needed
make lint

cp .env.example .env && $EDITOR .env    # add your provider key
make preflight   # verify the credential and list reachable models
make eval        # the 20-scenario evaluation suite
make serve       # FastAPI on :8000 — POST /runs
```

## Documentation

| Document | Contents |
|---|---|
| [`docs/roadmap.md`](docs/roadmap.md) | The 10 phases, deliverables, and acceptance criteria |
| [`docs/architecture.md`](docs/architecture.md) | Target architecture and what exists today |
| [`docs/design-decisions.md`](docs/design-decisions.md) | Decision log — what we chose and why |
| [`docs/phase-1.md`](docs/phase-1.md) | Phase 1: the hand-written agent loop |
| [`docs/phase-1.5.md`](docs/phase-1.5.md) | Phase 1.5 plan: the deployed scenario demo |
| [`evaluations/benchmarks/README.md`](evaluations/benchmarks/README.md) | Running the evaluation suite |
| [`evaluations/results/README.md`](evaluations/results/README.md) | Measured Phase 1 results and what the first live run exposed |

## Repository layout

```
backend/            FastAPI service + agent runtime
  app/agents/       Agent loop, prompts, termination logic
  app/tools/        Tool definitions, schemas, registry, permission checks
  app/memory/       Short-term, episodic, semantic, procedural memory (Phase 2)
  app/retrieval/    Chunking, embeddings, hybrid search, reranking (Phase 6)
  app/orchestration/ Coordinator and specialist agents (Phase 7)
  app/gateway/      Model routing, budgets, fallback (Phase 8)
  app/approvals/    Permissions, limits, idempotency, durable execution (Phase 9)
  app/observability/ Traces, spans, cost accounting (Phase 10)
frontend/           React + TypeScript dashboard and approval queue
skills/             Agent skills with progressive disclosure (Phase 5)
evaluations/        Scenario datasets, benchmark harness, recorded results
experiments/        Reproducible experiments (context, retrieval, orchestration)
data/               Seed business data and knowledge-base documents
docs/               Architecture, decisions, security, lessons learned
```

## License

MIT — see [LICENSE](LICENSE).
