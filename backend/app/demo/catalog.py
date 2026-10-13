"""The demo's scenario catalog: what a visitor may click, and what it means.

Three sources meet here, each owning what it is actually authoritative about:

    evaluations/datasets/...yaml   what the customer says, and what must happen
    data/seed/*.json              who the customer is and what they ordered
    data/demo/scenarios.yaml      which scenarios appear, in what order

The join lives in one place so the rest of the phase consumes a result instead
of re-deriving it. The alternative — a recorder with its own scenario list and a
page with another — ends with seven recordings behind eight buttons.

**The client sends a scenario id and never a message.** That is the control that
makes "fixed scenarios" structural rather than cosmetic (DD-011), and this
module is where it is enforced: `resolve()` accepts an id, looks it up in the
manifest, and raises on anything else. A crafted request has nothing to carry a
prompt in.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import REPO_ROOT, Settings, get_settings
from app.data.store import DataStore
from app.demo.replay import ReplayIndex
from app.evaluation.scenario import Dataset, Scenario, load_dataset

MANIFEST_PATH = REPO_ROOT / "data" / "demo" / "scenarios.yaml"

ENTRY_KEYS = frozenset({"scenario", "order", "headline", "why"})

#: Refund statuses the approval ladder can produce, in the words the page uses.
#: Derived from the dataset's expectations rather than written in the manifest,
#: so an expectation change cannot leave a stale promise on a public page.
OUTCOME_PHRASES = {
    "auto_approved": "Refund approved automatically",
    "pending_manager_approval": "Refund held for manager approval",
    "escalated_manual_handling": "Refund escalated for manual handling",
}
REFUND_CREATED = "Refund created"
NO_REFUND = "No refund created"


class DemoManifestError(Exception):
    """The manifest is malformed — a bug in the demo, not a failing agent."""


class UnknownDemoScenario(KeyError):
    """An id that is not on the demo's list. Raised rather than resolved."""


@dataclass(frozen=True)
class DemoScenario:
    """One clickable scenario: the manifest's choices joined to the dataset's."""

    id: str
    title: str
    headline: str
    why: str
    order_id: str
    message: str
    expected_outcome: str

    def view(
        self, store: DataStore, settings: Settings, replays: ReplayIndex | None = None
    ) -> dict[str, Any]:
        """The JSON the page renders.

        Hydrated per request rather than at load: a live run creates a refund,
        and `prior_refunds` has to reflect that the next time the page is loaded.
        """
        order = store.get_order(self.order_id)
        customer = store.get_customer(order.customer_id) if order else None
        if order is None or customer is None:  # pragma: no cover - load() rejects this
            raise DemoManifestError(f"scenario {self.id}: order {self.order_id!r} is not seeded")

        return {
            "id": self.id,
            "title": self.title,
            "headline": self.headline,
            "message": self.message,
            "customer": {
                "id": customer.customer_id,
                "name": customer.name,
                "tier": customer.tier,
            },
            "order": {
                "id": order.order_id,
                "status": order.status,
                "total_cents": order.total_cents,
                "currency": order.currency,
                "placed_at": order.placed_at.isoformat(),
                "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
                "delivered_days_ago": order.days_since_delivery(settings.simulated_today),
                "items": [{"name": item.name, "quantity": item.quantity} for item in order.items],
                "prior_refunds": len(store.refunds_for_order(order.order_id)),
            },
            "expected": {"outcome": self.expected_outcome, "why": self.why},
            # When the recording was captured and by which model, so a stale
            # replay is visible on screen rather than invisible (DD-011).
            "replay": (replays or ReplayIndex()).meta(self.id),
        }


@dataclass(frozen=True)
class DemoCatalog:
    version: int
    scenarios: tuple[DemoScenario, ...]

    def __len__(self) -> int:
        return len(self.scenarios)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(scenario.id for scenario in self.scenarios)

    def resolve(self, scenario_id: str) -> DemoScenario:
        """Look up a scenario the visitor asked for, or refuse to invent one."""
        for scenario in self.scenarios:
            if scenario.id == scenario_id:
                return scenario
        raise UnknownDemoScenario(scenario_id)

    def message_for(self, scenario_id: str) -> str:
        """The only way a demo run gets its prompt."""
        return self.resolve(scenario_id).message

    def view(
        self,
        store: DataStore,
        settings: Settings | None = None,
        replays: ReplayIndex | None = None,
    ) -> dict[str, Any]:
        settings = settings or get_settings()
        replays = replays or ReplayIndex()
        return {
            "version": self.version,
            # Business time is simulated against a fixed date (DD-008), so
            # "delivered 6 days ago" needs that date on screen beside it.
            "simulated_today": settings.simulated_today.isoformat(),
            "scenarios": [scenario.view(store, settings, replays) for scenario in self.scenarios],
        }


def load_catalog(
    manifest_path: Path | None = None,
    *,
    dataset: Dataset | None = None,
    store: DataStore | None = None,
) -> DemoCatalog:
    """Load and validate the manifest against the dataset and the seed data.

    Validation is strict and loud. Every failure here is a mistake that would
    otherwise reach a public page as a dead button, a stale price, or a scenario
    we know does not work yet.
    """
    raw = yaml.safe_load((manifest_path or MANIFEST_PATH).read_text())
    dataset = dataset or load_dataset()
    store = store or DataStore()

    entries = raw.get("scenarios") or []
    if not entries:
        raise DemoManifestError("manifest lists no scenarios")

    scenarios: list[DemoScenario] = []
    seen: set[str] = set()
    for entry in entries:
        scenario = _parse_entry(entry, dataset, store)
        if scenario.id in seen:
            raise DemoManifestError(f"scenario {scenario.id} is listed twice")
        seen.add(scenario.id)
        scenarios.append(scenario)

    return DemoCatalog(version=raw["version"], scenarios=tuple(scenarios))


@lru_cache(maxsize=1)
def get_catalog() -> DemoCatalog:
    """The process-wide catalog. The manifest does not change while serving."""
    return load_catalog()


# --- validation -------------------------------------------------------------


def _parse_entry(entry: dict[str, Any], dataset: Dataset, store: DataStore) -> DemoScenario:
    if unknown := set(entry) - ENTRY_KEYS:
        raise DemoManifestError(f"entry {entry.get('scenario')}: unknown key(s) {sorted(unknown)}")
    for required in sorted(ENTRY_KEYS):
        if not entry.get(required):
            raise DemoManifestError(f"entry {entry.get('scenario')}: missing {required!r}")

    scenario_id = entry["scenario"]
    try:
        scenario = dataset.get(scenario_id)
    except KeyError:
        raise DemoManifestError(
            f"entry {scenario_id}: no such scenario in the evaluation dataset"
        ) from None

    if scenario.is_known_gap:
        raise DemoManifestError(
            f"entry {scenario_id}: scenario is a known gap and must not be demonstrated "
            f"({scenario.known_gap})"
        )

    order_id = str(entry["order"])
    _check_order(scenario_id, order_id, scenario, store)

    return DemoScenario(
        id=scenario.id,
        title=scenario.title,
        headline=entry["headline"],
        why=entry["why"],
        order_id=order_id,
        message=scenario.message,
        expected_outcome=_expected_outcome(scenario),
    )


def _check_order(scenario_id: str, order_id: str, scenario: Scenario, store: DataStore) -> None:
    """The order has to exist, and has to be the one the scenario is about."""
    if store.get_order(order_id) is None:
        raise DemoManifestError(f"entry {scenario_id}: order {order_id!r} is not in the seed data")

    expected_order = _refund_expectation(scenario_id, scenario).get("order_id")
    if expected_order is not None and str(expected_order) != order_id:
        raise DemoManifestError(
            f"entry {scenario_id}: manifest shows order {order_id!r} but the scenario's "
            f"refund expectation is about order {str(expected_order)!r}"
        )


def _refund_expectation(scenario_id: str, scenario: Scenario) -> dict[str, Any]:
    refund = scenario.expect.get("refund")
    if refund is None:
        raise DemoManifestError(
            f"entry {scenario_id}: the scenario has no refund expectation, so the page "
            "has no outcome to state. Demo scenarios must assert a refund outcome"
        )
    if isinstance(refund, list):
        raise DemoManifestError(
            f"entry {scenario_id}: the scenario expects refunds on {len(refund)} orders. "
            "The page shows one order per scenario, so this one cannot be demonstrated as is"
        )
    return refund


def _expected_outcome(scenario: Scenario) -> str:
    """Derive the outcome line from what the evaluation suite already asserts.

    Authoring this sentence in the manifest would mean a page promising
    "approved automatically" long after the expectation changed to pending. The
    reason behind the outcome is not derivable and stays hand-written; the
    outcome itself is not ours to phrase freely.
    """
    refund = _refund_expectation(scenario.id, scenario)
    if "created" not in refund:
        raise DemoManifestError(
            f"entry {scenario.id}: refund expectation does not say whether a refund is "
            "created, so the expected outcome cannot be derived"
        )
    if not refund["created"]:
        return NO_REFUND

    status = refund.get("status")
    if status is None:
        return REFUND_CREATED
    if status not in OUTCOME_PHRASES:
        raise DemoManifestError(
            f"entry {scenario.id}: refund status {status!r} has no outcome phrase; add one "
            "to OUTCOME_PHRASES rather than letting the page show a raw status"
        )
    return OUTCOME_PHRASES[status]
