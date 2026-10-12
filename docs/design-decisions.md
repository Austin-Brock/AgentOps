# Design decisions

A running log of the choices that shaped this repository, each with the reasoning and the
alternatives considered. Entries are append-only; a superseded decision is marked, not deleted.

---

## DD-001 — Learning-first repository with a working product at the center

**Decision:** build an agent engineering laboratory where each concept has its own
implementation, experiments, benchmarks, and documentation. The customer support platform is the
shared test environment.

**Alternative:** product-first — build the support application and introduce AI concepts only as
the product needs them. That yields a more polished demo.

**Why:** the goal is not to show that an LLM can be wired into an application. It is to understand
agent architecture well enough to implement it independently and defend the design. A product-first
repo hides the measurements that make the understanding visible.

**Consequence:** `experiments/` and `evaluations/` are first-class directories, not afterthoughts.

---

## DD-002 — Write the first agent loop by hand; add LangGraph later for comparison

**Decision:** Phase 1 implements the agent loop in plain Python. No LangGraph, CrewAI, or
equivalent.

**Why:** the loop is where the understanding lives — what exactly passes between the model, the
tool definitions, the application code, and the tool results. A framework answers that question
for you and hides the answer.

**Consequence:** once the hand-written loop works, the same workflow is implemented with LangGraph
and the two are compared in the README. That comparison is a deliverable, not a detour.

---

## DD-003 — Anthropic as the model provider, behind a gateway seam

**Decision:** call Anthropic (`claude-opus-5`) directly in Phase 1, through a thin internal client
that owns model selection, usage accounting, and retries.

**Why:** a single provider keeps Phase 1 focused on the agent loop rather than on abstraction.
Isolating the call behind one module means Phase 8 can introduce LiteLLM routing without touching
agent or tool code.

**Consequence:** nothing outside `backend/app/llm/` knows which provider is in use.

---

## DD-004 — Safety rules live in application code, never in prompts

**Decision:** tool permissions, refund limits, and authorization checks are enforced in Python
before a tool executes. Prompts and skill files may describe them; they never implement them.

**Why:** a prompt is a request, not a constraint. An instruction embedded in a retrieved document
can argue with a prompt. It cannot argue with a permission check.

**Consequence:** every tool carries required scopes, and the executor denies calls the agent is not
granted — even when the model has clearly decided otherwise. The denial is returned to the model as
a tool error so it can adapt.

---

## DD-005 — The agent loop is testable without network access

**Decision:** the loop accepts an injectable model client. Tests drive it with scripted responses;
live-model scenarios are opt-in and run on demand.

**Why:** loop mechanics — termination, step ceilings, tool-error handling, permission denial,
message threading — are deterministic logic and deserve deterministic tests. Paying for tokens to
assert that a step counter increments is a bad trade, and a test suite that needs a key is a test
suite that does not run.

**Consequence:** two test tiers. Offline tests gate every change. Live scenario evaluation runs
against a real model and records its results as data under `evaluations/results/`.

---

## DD-006 — Simulated business data in Phase 1; PostgreSQL when memory needs it

**Decision:** orders, customers, and policies are seed fixtures in `data/seed/` for Phase 1.
PostgreSQL arrives in Phase 2 with episodic memory.

**Why:** Phase 1's subject is the loop. Introducing a database first would mean a schema designed
before the access patterns that justify it are known.

**Consequence:** tool implementations read through a small data-access layer so the storage swap in
Phase 2 is confined to that layer.

---

## DD-007 — Refunds are always simulated

**Decision:** no payment provider integration, in any phase. The refund executor is a simulator
that can be instructed to succeed, fail, or time out.

**Why:** the engineering lessons — idempotency keys, transaction status checks, bounded retries,
unknown-outcome handling — require a payment system that fails on command. A real one cannot be
asked to time out reproducibly.

**Consequence:** Phase 9's duplicate-execution test is a deterministic test rather than a story
about one.

---

## DD-008 — Business time is simulated against a fixed date

**Decision:** eligibility windows are computed against `settings.simulated_today`
(2026-10-12), not `date.today()`.

**Why:** the refund window is 30 days. With a real clock, the seed order that is "inside the
window" today is outside it next month, and a scenario suite that passes in October starts
failing in November for reasons that have nothing to do with the agent. A measurement you
cannot reproduce is not a measurement.

**Consequence:** the clock is a setting, so an experiment can move it deliberately — which is
how the window-boundary cases get tested. Real wall-clock time arrives with Phase 9's durable
execution, where elapsed time is part of what is being tested.
