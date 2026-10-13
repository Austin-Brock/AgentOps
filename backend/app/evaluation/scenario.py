"""Scenario loading and dataset validation.

The dataset is checked structurally by the offline suite even though running the
scenarios needs a model. A typo in an expectation key or an uncompilable regex
should fail in CI, not silently pass every scenario at evaluation time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import REPO_ROOT

DATASET_PATH = REPO_ROOT / "evaluations" / "datasets" / "phase1_support_scenarios.yaml"

#: Words that appear in nearly any support reply. A check resting on one of
#: these passes unconditionally, which is worse than having no check at all
#: because it reads as coverage.
VACUOUS_PATTERNS = frozenset(
    {"refund", "order", "system", "policy", "item", "items", "the", "a", "and", "you"}
)

EXPECTATION_KEYS = frozenset(
    {
        "status",
        "tools_executed_include",
        "tools_not_executed",
        "tools_attempted_max",
        "refund",
        "response_matches_any",
        "response_not_matches",
    }
)
REFUND_KEYS = frozenset({"order_id", "created", "status", "not_exceeding_cents"})
SCENARIO_KEYS = frozenset(
    {"id", "title", "message", "customer_id", "faults", "expect", "known_gap"}
)


class DatasetError(Exception):
    """The dataset itself is malformed — a bug in the suite, not a failing agent."""


@dataclass(frozen=True)
class FaultSpec:
    tool: str
    times: int = 1
    code: str = "upstream_timeout"


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    message: str
    expect: dict[str, Any]
    customer_id: str | None = None
    faults: tuple[FaultSpec, ...] = ()
    known_gap: str | None = None

    @property
    def is_known_gap(self) -> bool:
        return self.known_gap is not None


@dataclass(frozen=True)
class Dataset:
    version: int
    phase: int
    scenarios: tuple[Scenario, ...] = field(default_factory=tuple)

    def __len__(self) -> int:
        return len(self.scenarios)

    def get(self, scenario_id: str) -> Scenario:
        for scenario in self.scenarios:
            if scenario.id == scenario_id:
                return scenario
        raise KeyError(scenario_id)


def load_dataset(path: Path | None = None, *, known_tools: set[str] | None = None) -> Dataset:
    raw = yaml.safe_load((path or DATASET_PATH).read_text())
    scenarios: list[Scenario] = []
    seen: set[str] = set()

    for entry in raw.get("scenarios", []):
        scenario = _parse_scenario(entry)
        if scenario.id in seen:
            raise DatasetError(f"duplicate scenario id: {scenario.id}")
        seen.add(scenario.id)
        _validate_expectations(scenario, known_tools)
        scenarios.append(scenario)

    if not scenarios:
        raise DatasetError("dataset contains no scenarios")

    return Dataset(version=raw["version"], phase=raw["phase"], scenarios=tuple(scenarios))


def _parse_scenario(entry: dict[str, Any]) -> Scenario:
    unknown = set(entry) - SCENARIO_KEYS
    if unknown:
        raise DatasetError(f"scenario {entry.get('id')}: unknown key(s) {sorted(unknown)}")
    for required in ("id", "title", "message", "expect"):
        if required not in entry:
            raise DatasetError(f"scenario {entry.get('id')}: missing {required!r}")
    return Scenario(
        id=entry["id"],
        title=entry["title"],
        message=entry["message"],
        expect=entry["expect"],
        customer_id=entry.get("customer_id"),
        faults=tuple(FaultSpec(**f) for f in entry.get("faults", [])),
        known_gap=entry.get("known_gap"),
    )


def _validate_expectations(scenario: Scenario, known_tools: set[str] | None) -> None:
    where = f"scenario {scenario.id}"
    unknown = set(scenario.expect) - EXPECTATION_KEYS
    if unknown:
        raise DatasetError(f"{where}: unknown expectation key(s) {sorted(unknown)}")
    if not scenario.expect:
        raise DatasetError(f"{where}: no expectations, so it can never fail")

    for key in ("response_matches_any", "response_not_matches"):
        for pattern in scenario.expect.get(key, []):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise DatasetError(
                    f"{where}: {key} pattern {pattern!r} does not compile: {exc}"
                ) from exc
            if pattern.strip().lower() in VACUOUS_PATTERNS:
                raise DatasetError(
                    f"{where}: {key} pattern {pattern!r} appears in nearly any reply "
                    "and asserts nothing; assert a distinctive phrase or use a state check"
                )
            if not pattern.isascii():
                # The grader folds typographic punctuation to ASCII before
                # matching, so a pattern containing a curly quote or en dash can
                # never match anything.
                raise DatasetError(
                    f"{where}: {key} pattern {pattern!r} contains non-ASCII characters; "
                    "response text is normalized to ASCII punctuation before matching"
                )

    refund = scenario.expect.get("refund")
    if refund is not None:
        for entry in refund if isinstance(refund, list) else [refund]:
            unknown_refund = set(entry) - REFUND_KEYS
            if unknown_refund:
                raise DatasetError(f"{where}: unknown refund key(s) {sorted(unknown_refund)}")
            if "order_id" not in entry:
                raise DatasetError(f"{where}: refund expectation needs an order_id")

    if known_tools is None:
        return
    referenced = {
        *scenario.expect.get("tools_executed_include", []),
        *scenario.expect.get("tools_not_executed", []),
        *scenario.expect.get("tools_attempted_max", {}),
        *(f.tool for f in scenario.faults),
    }
    if missing := referenced - known_tools:
        raise DatasetError(f"{where}: references unknown tool(s) {sorted(missing)}")
