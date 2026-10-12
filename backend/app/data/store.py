"""Simulated business data access.

Phase 1 reads JSON fixtures. Tool implementations only ever touch this module,
so swapping in PostgreSQL during Phase 2 is confined to one file (DD-006).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings


def _parse_date(value: str | None) -> date | None:
    return datetime.strptime(value, "%Y-%m-%d").date() if value else None


@dataclass(frozen=True)
class OrderItem:
    sku: str
    name: str
    quantity: int
    unit_price_cents: int


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    status: str
    placed_at: date
    delivered_at: date | None
    total_cents: int
    currency: str
    items: tuple[OrderItem, ...]
    carrier: str | None
    tracking_number: str | None

    def days_since_delivery(self, today: date) -> int | None:
        if self.delivered_at is None:
            return None
        return (today - self.delivered_at).days


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    email: str
    tier: str
    preferred_channel: str


@dataclass
class Refund:
    refund_id: str
    order_id: str
    customer_id: str
    amount_cents: int
    reason: str
    status: str
    created_at: str
    idempotency_key: str


@dataclass
class DataStore:
    """In-process store. Construct a fresh one per test for isolation."""

    settings: Settings = field(default_factory=get_settings)
    orders: dict[str, Order] = field(default_factory=dict)
    customers: dict[str, Customer] = field(default_factory=dict)
    refunds: dict[str, Refund] = field(default_factory=dict)
    _refund_seq: int = 0

    def __post_init__(self) -> None:
        if not self.orders:
            self._load_seed()

    # --- loading ---

    def _read(self, name: str) -> list[dict[str, Any]]:
        path: Path = self.settings.seed_dir / f"{name}.json"
        return json.loads(path.read_text())

    def _load_seed(self) -> None:
        for raw in self._read("orders"):
            placed = _parse_date(raw["placed_at"])
            assert placed is not None
            self.orders[raw["order_id"]] = Order(
                order_id=raw["order_id"],
                customer_id=raw["customer_id"],
                status=raw["status"],
                placed_at=placed,
                delivered_at=_parse_date(raw["delivered_at"]),
                total_cents=raw["total_cents"],
                currency=raw["currency"],
                items=tuple(OrderItem(**item) for item in raw["items"]),
                carrier=raw["carrier"],
                tracking_number=raw["tracking_number"],
            )
        for raw in self._read("customers"):
            self.customers[raw["customer_id"]] = Customer(
                customer_id=raw["customer_id"],
                name=raw["name"],
                email=raw["email"],
                tier=raw["tier"],
                preferred_channel=raw["preferred_channel"],
            )
        for raw in self._read("refunds"):
            self.refunds[raw["refund_id"]] = Refund(**raw)
            self._refund_seq = max(self._refund_seq, int(raw["refund_id"].split("-")[1]))

    # --- reads ---

    def get_order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id.lstrip("#").strip())

    def get_customer(self, customer_id: str) -> Customer | None:
        return self.customers.get(customer_id)

    def refunds_for_order(self, order_id: str) -> list[Refund]:
        oid = order_id.lstrip("#").strip()
        return [r for r in self.refunds.values() if r.order_id == oid]

    def find_refund_by_idempotency_key(self, key: str) -> Refund | None:
        return next((r for r in self.refunds.values() if r.idempotency_key == key), None)

    # --- writes ---

    def create_refund(
        self,
        *,
        order_id: str,
        customer_id: str,
        amount_cents: int,
        reason: str,
        status: str,
        idempotency_key: str,
    ) -> Refund:
        self._refund_seq += 1
        refund = Refund(
            refund_id=f"RF-{self._refund_seq:04d}",
            order_id=order_id,
            customer_id=customer_id,
            amount_cents=amount_cents,
            reason=reason,
            status=status,
            created_at=self.settings.simulated_today.isoformat(),
            idempotency_key=idempotency_key,
        )
        self.refunds[refund.refund_id] = refund
        return refund

    # --- documents ---

    def read_document(self, filename: str) -> str:
        return (self.settings.documents_dir / filename).read_text()
