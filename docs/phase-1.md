# Phase 1 — Agent from scratch

The agent loop, written by hand. No orchestration framework, no database, no retrieval —
a model, three business tools, and the application code that decides what the model is
allowed to do with them.

## What runs

```
backend/app/
  agents/support_agent.py   the loop
  agents/prompts.py         instructions (what for, what not to do — not a script)
  llm/client.py             provider seam: ModelClient protocol + Anthropic implementation
  llm/scripted.py           scripted + looping test doubles
  tools/base.py             Tool, ToolContext, ToolError, fault injection
  tools/registry.py         what is advertised to the model
  tools/executor.py         authorize -> validate -> execute -> turn failures into data
  tools/order_tools.py      lookup_order
  tools/policy_tools.py     get_refund_policy
  tools/refund_tools.py     create_refund_request, with the limits enforced in code
  tools/restricted_tools.py apply_account_credit — visible, deliberately unauthorized
  evaluation/              scenario loading, deterministic grading, runner CLI
  observability/trace.py    ordered record of every run
  main.py                   FastAPI: /health, /tools, /runs
```

## Try it

```bash
cd backend
uv venv --python 3.11 .venv && uv pip install -e '.[dev]'

.venv/bin/python -m pytest                      # 62 tests, no API key needed
.venv/bin/ruff check . && .venv/bin/ruff format --check .

export ANTHROPIC_API_KEY=...                    # needed from here down
.venv/bin/python -m app.evaluation.run          # the 20 scenarios
.venv/bin/uvicorn app.main:app --reload         # then POST /runs
```

## The five challenges

| # | Challenge | Where it is tested |
|---|---|---|
| 1 | Tool calling | `tests/test_agent_loop.py::TestChallenge1ToolCalling` |
| 2 | Multi-step reasoning | `TestChallenge2MultiStepReasoning` |
| 3 | Failure recovery | `TestChallenge3FailureRecovery` |
| 4 | Guardrails | `TestChallenge4Guardrails`, `tests/test_executor.py::TestAuthorization` |
| 5 | Evaluation | `evaluations/datasets/phase1_support_scenarios.yaml` + `tests/test_evaluation.py` |

## Decisions worth knowing before reading the code

**The loop is bounded.** `max_steps` caps model calls per run. A model that asks for the
same tool forever exits as `max_steps_exceeded` rather than running up a bill —
`test_step_ceiling_stops_a_runaway_agent` proves it with a client that never stops asking.

**Authorization happens before argument parsing.** The executor's check order is: does the
tool exist, is the agent authorized, do the arguments validate, then run. A denied call
cannot have side effects because the handler is not reachable. The handler for
`apply_account_credit` raises if it is ever called, so a regression in the permission check
surfaces as a failing test rather than as money moving.

**Discovery is not authorization.** `apply_account_credit` is advertised to the model even
though the support agent lacks `finance:write`. Hiding it would make the permission
boundary untestable end to end — and Phase 4 has to defend the same boundary across MCP,
where tool discovery is the entire point.

**Refund rules are re-checked in code.** `create_refund_request` independently verifies the
order exists, was delivered, is inside the window, has not been refunded, and that the
amount does not exceed what was paid — regardless of what the model concluded from the
policy document. Amounts above the automatic limit are created *pending*, not released.

**Tool failures are data.** Every failure — unknown tool, bad arguments, permission denial,
business rejection, injected timeout — returns to the model as an error `tool_result` with
a code and a message. The agent reads it and adapts. `FaultInjector` schedules failures in
advance so recovery is a test rather than a hope.

**Parallel tool results return in one turn.** When the model requests several tools at once,
all results come back in a single user message. Splitting them across turns teaches the
model to stop asking for tools in parallel.

**The clock is fixed.** Business data is evaluated against `simulated_today`
(2026-10-12) so that eligibility windows — and therefore evaluation results — do not drift
as real time passes.

## Deliberately absent

Per-customer authorization on reads (scenario S11 tracks this as a known gap for Phase 10),
persistence, conversation memory, retrieval, approval workflow, and cost routing. Each
arrives in the phase that demonstrates why it is needed.
