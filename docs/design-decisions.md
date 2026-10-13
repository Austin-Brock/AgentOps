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

---

## DD-009 — Both providers, and an honestly neutral seam

**Supersedes DD-003.**

**What DD-003 got wrong, in two parts.**

First, the provider choice. DD-003 presented Anthropic as a reasoned decision. It was not: the
plan said "OpenAI or Anthropic," no credential was present to infer from, and the Anthropic API
reference was loaded in the authoring context. That is a default, not an analysis, and writing it
up as the latter made it harder to revisit. What is actually defensible is *one provider for Phase
1* — it keeps the phase about the agent loop instead of about abstraction layers. Which one was
arbitrary.

Second, the seam. DD-003 claimed nothing outside `app/llm/` knew the provider. Two places did:
the agent loop built tool-result messages in Anthropic's wire format, and the tool executor
stamped Anthropic's `tool_result` block type. The seam covered the *request*, not the
*conversation* — which is the half that actually differs:

| | Anthropic | OpenAI |
|---|---|---|
| System prompt | top-level field | a message in the array |
| Tool specs | `{name, description, input_schema}` | nested under `{"type": "function", ...}` |
| Tool calls | blocks inside assistant content | `tool_calls` on the assistant message |
| Tool results | one user message, N `tool_result` blocks | N messages, `role: "tool"`, one id each |
| Arguments | a parsed object | a JSON **string**, not guaranteed to parse |

**Decision:** support both providers. The `ModelClient` protocol now owns message construction —
`create()` for the request, `tool_result_messages()` for the results — so the loop appends what
the adapter hands it and never inspects either. Provider stop reasons are normalized onto one
vocabulary so termination logic is written once. `app/llm/factory.py` is the single place a
provider name maps to an implementation.

**Why both rather than swapping:** Phase 8's deliverable *is* multi-provider routing and
fallback, so a second adapter is work that phase needs regardless. Phase 3's experiments get a
second model to compare against. And the cost was low — the whole coupling was ~160 lines of
adapter plus two leaked lines, out of ~2,250 lines of application code. The tools, authorization,
refund rules, and the entire test suite were already provider-neutral.

**Consequence:** the neutrality claim is now tested rather than asserted. `ScriptedModelClient`
takes a wire format, so `tests/test_providers.py::TestLoopIsProviderNeutral` drives the same
scripted run through both and asserts each produces its provider's threading *and* identical
agent behavior. Adapters also normalize *when* a missing credential surfaces: the OpenAI SDK
raises at construction while Anthropic defers to request time, so the OpenAI adapter catches that
and reports it through `credential_available()` instead. Without this, the same missing-key
mistake was a clean 503 on one provider and a 500 on the other.
