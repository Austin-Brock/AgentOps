# Benchmarks

## Phase 1 — support scenario evaluation

Twenty scenarios, graded by deterministic assertions only. Dataset:
[`../datasets/phase1_support_scenarios.yaml`](../datasets/phase1_support_scenarios.yaml).

```bash
cd backend
.venv/bin/python -m app.evaluation.run                 # all scenarios
.venv/bin/python -m app.evaluation.run --only S01 S05  # a subset
.venv/bin/python -m app.evaluation.run --list          # no model calls
.venv/bin/python -m app.evaluation.run --effort low --concurrency 5
```

Requires `ANTHROPIC_API_KEY`. Each run writes a JSON record to `../results/` and exits
non-zero if any gated scenario fails, so it works as a pre-merge gate.

### What it measures

| | |
|---|---|
| Tool selection | Which tools ran, which were refused, how many attempts each took |
| Outcome | Whether a refund was created, at what approval status, for how much |
| Safety | That forbidden tools never executed and limits were never exceeded |
| Response content | Regex assertions on the customer-facing message |
| Cost | Input and output tokens per scenario and per run |

Scenarios marked `known_gap` are run and reported but excluded from the pass gate. Each one
names the phase that closes it — the suite tracks acknowledged debt instead of hiding it.

### What it does not measure

Response quality, tone, and helpfulness are not asserted here. Judging those needs an
LLM grader, which Phase 10 adds as a **separate** tier so that business rules and security
properties never depend on a model's opinion.
