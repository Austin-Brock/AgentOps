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

---

## DD-010 — Assert state, not wording

**Decision:** scenario expectations prefer observable state — which tools ran, what the store
contains afterwards, what status a refund carries — over regular expressions on the agent's
prose. Wording checks stay, but as a secondary signal, and each must assert a property that is
stable across phrasings rather than a specific phrasing.

**Why:** the first five live runs made the cost concrete. Every reply contained typographic
punctuation, so an ASCII pattern failed on a correct answer. A "not found" check was widened
twice and still failed three different correct wordings. Two checks forbade *mentioning* a topic
and so penalized the refusal they existed to verify. Meanwhile every state check — no refund
created outside the window, the finance tool never executed, the amount never exceeding what was
paid — was right every time, including on the runs where the wording checks were wrong.

The asymmetry is structural. A refund row either exists or it does not. A sentence can be correct
in unbounded ways, so a pattern over prose is a guess about vocabulary, and a guess that is wrong
in the lenient direction is worse than no check at all — it reads as coverage.

**Consequence:** the grader normalizes typographic punctuation before matching, and the dataset
loader rejects two classes of pattern outright: non-ASCII ones, which can never match normalized
text, and bare common words like `refund` or `system`, which match nearly any reply. The second
guard immediately failed two scenarios of my own that were passing on `refund` and `system` —
checks that had been asserting nothing while appearing to pass.

---

## DD-011 — Pull the demo UI ahead of memory

**Decision:** insert a deployed scenario demo (Phase 1.5) between Phases 1 and 2, with fixed
scenarios only, replay by default, and a rate-limited live mode. Hosted on DigitalOcean App
Platform.

**Why:** after Phase 1 the only ways to see the agent work were reading the code or running
`make demo` in a terminal. Both filter out almost everyone. The guardrail behavior in particular
— the agent reaching for a tool it is not allowed to use, being refused, and recovering — is the
most interesting thing the project does and the least visible.

It is also cheaper to solve now than later. A public endpoint raises hosting, streaming, cost
control, and abuse questions that are easier to answer against a four-tool agent than against one
with memory, retrieval, and a multi-agent coordinator behind it. And every subsequent phase gets a
surface to be visible on: memory as *"Dana contacted us about this last week,"* retrieval as
citations a visitor can open, approvals as a queue they can act on.

**What it costs:** Phase 2 slips, and there is now a deployment to keep working across eight more
phases of change. The second cost is partly a benefit — it forces deployment discipline early
rather than discovering it in Phase 10.

**Fixed scenarios rather than free text:** a public endpoint accepting arbitrary text is both an
open tap on the API budget and a prompt-injection surface. The control is structural rather than
cosmetic — the client sends a scenario id and the server resolves the message from the evaluation
dataset, so a crafted request cannot smuggle in its own prompt. Free text is worth adding later,
and the injection scenario is the best demonstration in the project, but it ships on a foundation
that cannot be drained.

**Replay by default rather than live:** recorded traces cost nothing, are deterministic, and work
with no credentials at all — so the demo cannot break because a key expired or a rate limit was
hit. A live button exists to answer the obvious objection that a replay might be a video.

**Consequence:** the demo must derive its scenarios from
`evaluations/datasets/phase1_support_scenarios.yaml` rather than keeping its own copy. Two
definitions of the same scenario will drift, and the one on the public page is the one that
matters. Recorded replays carry the git SHA and model they were captured under, because a page
quietly demonstrating last month's agent is worse than no page.

---

## DD-012 — Stream the run by subscribing to the trace, not by duplicating the loop

**Decision:** `SupportAgent.run()` takes an optional `on_event` subscriber, which is attached to
the run's `RunTrace`. Every event recorded during the run — by the loop and by the tool executor —
is published to the subscriber as it is recorded. `TraceEvent` gained a structured `data` field
that `render()` ignores, carrying the arguments, results, and final text a viewer needs but a log
line should not contain.

**Alternative considered:** a parallel `run_events()` async generator, as Phase 1.5 noted. Cleaner
for consumers, but it means two implementations of the loop, and the loop is the most-tested code
in the repository. Duplicating it would halve the value of those tests on the day the copies
diverged.

**Alternative considered:** a second event type emitted alongside the trace from the loop. That
keeps the stream's shape independent of the log's, at the cost of two statements per moment and a
standing invitation to update one and not the other. The trace already *is* the ordered record of
what the agent did; a live viewer wants that same record sooner, not a different one.

**Why the subscriber sits on the trace:** the tool executor records the three events that matter
most to the demo — `tool_refused`, `tool_succeeded`, `tool_failed` — and it is the layer holding
the arguments and the payload. Publishing from the trace means those events stream from where they
happen, with no coordination between layers and nothing for the loop to re-emit.

**The subscriber is synchronous.** `RunTrace.record()` is called from synchronous code in the
executor, so an async subscriber could not be awaited at the call sites that matter. Sync is also
what the streaming consumer wants: an SSE endpoint hands each event to `asyncio.Queue.put_nowait`.

**A failing subscriber is detached, not raised.** A browser closing its connection mid-run must not
take the run down with it, and a trace full of identical plumbing errors would bury the run it
describes. The first failure drops the subscriber and is recorded on `trace.subscriber_error` for
the caller to inspect.

**Consequence:** `run_finished` moved into `SupportAgent._finish`, so all four exit paths —
completed, truncated, provider refusal, step ceiling — record it in one place. No run can now end
without a subscriber seeing it end. The HTTP `/runs` response is unchanged: it renders `detail`
only, so `data` stays out of the existing API until the demo endpoints ask for it.

---

## DD-013 — The demo states an expectation it does not author

**Decision:** the demo's scenario list lives in `data/demo/scenarios.yaml` and holds presentation
only — which scenarios appear, in what order, a short headline, and the one clause of reasoning
that cannot be derived. It holds no customer message and nothing that can be looked up. The
expected *outcome* is derived from the evaluation dataset's own expectations; the page renders it
beside the actual outcome taken from the run's trace.

**Alternative considered:** annotate the evaluation dataset with a `demo:` block per scenario. One
file, no possibility of drift. Rejected because it gives a file whose job is expectations a second
job as a public page's layout, and makes an edit to a test able to change what a visitor sees.
DD-011's actual requirement is that the *message* is single-sourced, and it is: the manifest names
a scenario id and the message is resolved from the dataset. A test asserts the manifest contains no
message text, and another asserts every id it names exists and is not a `known_gap`.

**Why the outcome is derived rather than written:** a hand-written label reading "refund approved
automatically" survives the day the expectation changes to pending, and a public page confidently
describing behavior the agent no longer has is worse than no page. So `auto_approved`,
`pending_manager_approval`, and `escalated_manual_handling` map to phrases in code, an unmapped
status is a load-time failure rather than a raw enum on screen, and the only hand-written sentence
is the reason — which is domain knowledge and genuinely not derivable.

**Why expected and actual are shown separately:** several scenarios have more than one legitimate
path. On the out-of-window refund the agent may decline from the policy, or request the refund and
be rejected by `create_refund_request`; both satisfy the evaluation. A page that pre-labels the
outcome would eventually contradict the trace printed underneath it. Showing the dataset's
expectation above the trace's result turns the demo into a prediction being verified, and on the
occasions where a live run takes the other path, the page reports that honestly rather than only
ever agreeing with itself.

**Nothing typed that can be looked up:** the customer's name, the order total and status, how long
ago it was delivered, and how many refunds already exist are hydrated from `data/seed` per request.
Per request rather than at startup, because a live run creates a refund and the next page load has
to show it.

**Demo runs pass no `customer_id`,** matching how the evaluation suite runs the same scenarios. The
persona is presented as whose order it is — which is what the agent discovers through
`lookup_order` — rather than handed to the agent up front. A demo run that differs from the
evaluated run is a demo that proves something other than what was measured.

**Consequence:** the manifest is validated strictly at load. A listed id must exist in the dataset,
must not be a known gap, must assert a refund outcome on exactly one order, and that order must be
both in the seed data and the one the scenario's refund expectation names. Each of those failures
would otherwise reach a public page as a dead button, a stale price, or a demonstration of a
control the project does not yet have.
