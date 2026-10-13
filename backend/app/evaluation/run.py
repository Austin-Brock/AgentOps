"""Phase 1 scenario evaluation runner.

    python -m app.evaluation.run                    # all 20 scenarios
    python -m app.evaluation.run --only S01 S05     # a subset
    python -m app.evaluation.run --list             # no model calls

Needs ANTHROPIC_API_KEY. Results are written to evaluations/results/ as data so
that a change in prompt, tool, or model can be compared against the last run
rather than argued about.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agents.support_agent import SupportAgent
from app.config import REPO_ROOT, Settings
from app.data.store import DataStore
from app.evaluation.grader import ScenarioResult, grade
from app.evaluation.scenario import Dataset, Scenario, load_dataset
from app.llm.factory import build_model_client, credential_available
from app.tools.base import Fault, FaultInjector
from app.tools.toolset import build_support_toolset

RESULTS_DIR = REPO_ROOT / "evaluations" / "results"


@dataclass
class Summary:
    model: str
    effort: str
    started_at: str
    results: list[ScenarioResult] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def gated(self) -> list[ScenarioResult]:
        return [r for r in self.results if r.counts_toward_gate]

    @property
    def known_gaps(self) -> list[ScenarioResult]:
        return [r for r in self.results if not r.counts_toward_gate]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.gated if r.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / len(self.gated) if self.gated else 0.0

    @property
    def input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.results)

    @property
    def output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": 1,
            "model": self.model,
            "effort": self.effort,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 1),
            "scenarios": len(self.results),
            "gated": len(self.gated),
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 3),
            "known_gaps": [r.scenario_id for r in self.known_gaps],
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            },
            "results": [r.to_dict() for r in self.results],
        }


async def run_scenario(scenario: Scenario, settings: Settings) -> ScenarioResult:
    """Each scenario gets its own store, so refunds never leak between them."""
    store = DataStore(settings=settings)
    agent = SupportAgent(
        model=build_model_client(settings),
        registry=build_support_toolset(),
        store=store,
        settings=settings,
        faults=(
            FaultInjector([Fault(tool=f.tool, times=f.times, code=f.code) for f in scenario.faults])
            if scenario.faults
            else None
        ),
    )

    refunds_before = {}
    if refund_expect := scenario.expect.get("refund"):
        entries = refund_expect if isinstance(refund_expect, list) else [refund_expect]
        for entry in entries:
            order_id = entry["order_id"]
            refunds_before[order_id] = len(store.refunds_for_order(order_id))

    started = time.perf_counter()
    try:
        run = await agent.run(scenario.message, customer_id=scenario.customer_id)
    except Exception as exc:  # noqa: BLE001 - one bad scenario must not end the suite
        return ScenarioResult(
            scenario_id=scenario.id,
            title=scenario.title,
            known_gap=scenario.known_gap,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    result = grade(scenario, run, store, refunds_before=refunds_before)
    result.duration_ms = (time.perf_counter() - started) * 1000
    return result


async def run_dataset(dataset: Dataset, settings: Settings, *, concurrency: int) -> Summary:
    summary = Summary(
        model=settings.model,
        effort=settings.effort,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    semaphore = asyncio.Semaphore(concurrency)
    started = time.perf_counter()

    async def guarded(scenario: Scenario) -> ScenarioResult:
        async with semaphore:
            result = await run_scenario(scenario, settings)
            print(_line(result), flush=True)
            return result

    summary.results = list(await asyncio.gather(*(guarded(s) for s in dataset.scenarios)))
    summary.results.sort(key=lambda r: r.scenario_id)
    summary.duration_s = time.perf_counter() - started
    return summary


def _line(result: ScenarioResult) -> str:
    if result.error:
        mark = "ERROR"
    elif not result.counts_toward_gate:
        mark = "pass (gap)" if result.passed else "GAP "
    else:
        mark = "pass" if result.passed else "FAIL"
    detail = result.error or "; ".join(f"{c.name}: {c.detail}" for c in result.failures)
    return f"  {result.scenario_id}  {mark:<10} {result.title[:54]:<54} {detail}"


def report(summary: Summary) -> str:
    lines = [
        "",
        f"Phase 1 evaluation — {summary.model} (effort={summary.effort})",
        "=" * 78,
        f"  gated scenarios : {summary.passed}/{len(summary.gated)} ({summary.pass_rate:.0%})",
        f"  known gaps      : {', '.join(r.scenario_id for r in summary.known_gaps) or 'none'}",
        f"  tokens          : {summary.input_tokens:,} in / {summary.output_tokens:,} out",
        f"  wall clock      : {summary.duration_s:.1f}s",
    ]
    if failures := [r for r in summary.gated if not r.passed]:
        lines.append("")
        lines.append("  Failures:")
        for result in failures:
            lines.append(f"    {result.scenario_id} {result.title}")
            lines.append(f"        tools executed: {result.tools_executed or '(none)'}")
            if result.tools_refused:
                lines.append(f"        tools refused : {result.tools_refused}")
            for check in result.failures:
                lines.append(f"        - {check.name}: {check.detail}")
            if result.error:
                lines.append(f"        - error: {result.error}")
            # Show the evidence. Without it, diagnosing a wording failure costs
            # another live run, which is how the same bad assertion got
            # "fixed" twice.
            if result.final_text:
                lines.append("        final message:")
                lines.extend(
                    f"        | {line}" for line in _excerpt(result.final_text).splitlines()
                )
    return "\n".join(lines)


def _excerpt(text: str, limit: int = 900) -> str:
    return text if len(text) <= limit else text[:limit] + "\n... [truncated]"


def write_results(summary: Summary) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"phase1-{stamp}.json"
    path.write_text(json.dumps(summary.to_dict(), indent=2) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Phase 1 scenario evaluation.")
    parser.add_argument("--only", nargs="*", metavar="ID", help="Run only these scenario ids.")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--list", action="store_true", help="List scenarios and exit.")
    parser.add_argument("--no-write", action="store_true", help="Skip writing a results file.")
    args = parser.parse_args(argv)

    registry = build_support_toolset()
    dataset = load_dataset(known_tools=set(registry.tools))

    if args.only:
        wanted = set(args.only)
        selected = tuple(s for s in dataset.scenarios if s.id in wanted)
        if missing := wanted - {s.id for s in selected}:
            parser.error(f"unknown scenario id(s): {sorted(missing)}")
        dataset = Dataset(version=dataset.version, phase=dataset.phase, scenarios=selected)

    if args.list:
        for scenario in dataset.scenarios:
            gap = "  [known gap]" if scenario.is_known_gap else ""
            print(f"  {scenario.id}  {scenario.title}{gap}")
        return 0

    overrides: dict[str, Any] = {}
    if args.model:
        overrides["model"] = args.model
    if args.effort:
        overrides["effort"] = args.effort
    if args.max_steps:
        overrides["max_steps"] = args.max_steps
    settings = Settings(**overrides)

    if not credential_available(build_model_client(settings)):
        parser.error(
            f"No {settings.provider} credential resolved. Run `make preflight` for details."
        )

    print(f"Running {len(dataset)} scenario(s) against {settings.model} ({settings.provider})...\n")
    summary = asyncio.run(run_dataset(dataset, settings, concurrency=args.concurrency))
    print(report(summary))

    if not args.no_write:
        print(f"\n  results -> {write_results(summary).relative_to(REPO_ROOT)}")

    return 0 if summary.passed == len(summary.gated) else 1


if __name__ == "__main__":
    sys.exit(main())
