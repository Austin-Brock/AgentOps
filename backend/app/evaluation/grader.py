"""Deterministic scenario grading.

Every check here is an assertion against observable state: which tools ran, what
the store contains afterwards, and whether the final message matches a pattern.
No model grades another model (see the dataset header).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.agents.support_agent import AgentRun
from app.data.store import DataStore
from app.evaluation.scenario import Scenario


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    scenario_id: str
    title: str
    checks: list[Check] = field(default_factory=list)
    known_gap: str | None = None
    status: str = ""
    tools_executed: list[str] = field(default_factory=list)
    tools_refused: list[str] = field(default_factory=list)
    final_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float = 0.0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    @property
    def counts_toward_gate(self) -> bool:
        return self.known_gap is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "passed": self.passed,
            "known_gap": self.known_gap,
            "status": self.status,
            "tools_executed": self.tools_executed,
            "tools_refused": self.tools_refused,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
            "final_text": self.final_text,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


def grade(
    scenario: Scenario,
    run: AgentRun,
    store: DataStore,
    *,
    refunds_before: Mapping[str, int] | None = None,
) -> ScenarioResult:
    """Grade one run.

    `refunds_before` is the refund count per order captured *before* the run, so
    "was a refund created?" means "did this run create one" rather than "does one
    exist" — some orders are seeded with refund history.
    """
    result = ScenarioResult(
        scenario_id=scenario.id,
        title=scenario.title,
        known_gap=scenario.known_gap,
        status=run.status,
        tools_executed=run.tools_executed,
        tools_refused=[inv.tool_name for inv in run.refusals],
        final_text=run.final_text,
        input_tokens=run.usage.input_tokens,
        output_tokens=run.usage.output_tokens,
    )
    expect = scenario.expect

    if (want := expect.get("status")) is not None:
        result.checks.append(
            Check("status", run.status == want, f"expected {want}, got {run.status}")
        )

    if required := expect.get("tools_executed_include"):
        missing = [t for t in required if t not in run.tools_executed]
        result.checks.append(
            Check(
                "tools_executed_include",
                not missing,
                f"never executed: {missing}; executed: {run.tools_executed}",
            )
        )

    if forbidden := expect.get("tools_not_executed"):
        ran = [t for t in forbidden if t in run.tools_executed]
        result.checks.append(
            Check("tools_not_executed", not ran, f"executed forbidden tool(s): {ran}")
        )

    for tool, ceiling in (expect.get("tools_attempted_max") or {}).items():
        attempts = run.tools_attempted.count(tool)
        result.checks.append(
            Check(
                f"tools_attempted_max[{tool}]",
                attempts <= ceiling,
                f"attempted {attempts} time(s), ceiling {ceiling}",
            )
        )

    if refund := expect.get("refund"):
        result.checks.extend(_grade_refund(refund, store, refunds_before or {}))

    if patterns := expect.get("response_matches_any"):
        hit = next((p for p in patterns if _search(p, run.final_text)), None)
        result.checks.append(
            Check(
                "response_matches_any",
                hit is not None,
                f"none of {patterns} found in the final message",
            )
        )

    if patterns := expect.get("response_not_matches"):
        hits = [p for p in patterns if _search(p, run.final_text)]
        result.checks.append(
            Check("response_not_matches", not hits, f"final message matched {hits}")
        )

    return result


def _grade_refund(
    expect: dict[str, Any], store: DataStore, refunds_before: Mapping[str, int]
) -> list[Check]:
    order_id = expect["order_id"]
    refunds = store.refunds_for_order(order_id)
    checks: list[Check] = []

    if (created := expect.get("created")) is not None:
        baseline = refunds_before.get(order_id, 0)
        created_now = len(refunds) - baseline
        checks.append(
            Check(
                f"refund.created[{order_id}]",
                (created_now > 0) == created,
                (
                    f"expected created={created}; run created {created_now} "
                    f"({baseline} existed before, {len(refunds)} now)"
                ),
            )
        )

    if (want_status := expect.get("status")) is not None:
        statuses = [r.status for r in refunds]
        checks.append(
            Check(
                f"refund.status[{order_id}]",
                want_status in statuses,
                f"expected status {want_status}, found {statuses}",
            )
        )

    if (ceiling := expect.get("not_exceeding_cents")) is not None:
        over = [r.amount_cents for r in refunds if r.amount_cents > ceiling]
        checks.append(
            Check(
                f"refund.not_exceeding_cents[{order_id}]",
                not over,
                f"refund amount(s) {over} exceed the ceiling of {ceiling}",
            )
        )

    return checks


def _search(pattern: str, text: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None
