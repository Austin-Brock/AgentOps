"""Order lookup.

Returns facts only. Eligibility is a policy judgment the agent has to make from
the policy document, which is what makes the multi-step reasoning real rather
than a single tool call that answers the whole question.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.tools.base import Tool, ToolContext, ToolError


class LookupOrderInput(BaseModel):
    order_id: str = Field(
        description=(
            "The order number, digits only or with a leading '#'. Never guess "
            "this value — if the customer has not given it, ask them."
        )
    )


async def lookup_order(args: LookupOrderInput, ctx: ToolContext) -> dict[str, Any]:
    order = ctx.store.get_order(args.order_id)
    if order is None:
        raise ToolError(
            "order_not_found",
            f"No order matches {args.order_id!r}.",
            order_id=args.order_id,
        )

    today = ctx.settings.simulated_today
    existing = ctx.store.refunds_for_order(order.order_id)
    customer = ctx.store.get_customer(order.customer_id)

    return {
        "order_id": order.order_id,
        "customer_id": order.customer_id,
        "customer_name": customer.name if customer else None,
        "status": order.status,
        "placed_at": order.placed_at.isoformat(),
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "days_since_delivery": order.days_since_delivery(today),
        "total_cents": order.total_cents,
        "currency": order.currency,
        "items": [
            {
                "sku": item.sku,
                "name": item.name,
                "quantity": item.quantity,
                "unit_price_cents": item.unit_price_cents,
            }
            for item in order.items
        ],
        "carrier": order.carrier,
        "tracking_number": order.tracking_number,
        "existing_refunds": [
            {
                "refund_id": r.refund_id,
                "amount_cents": r.amount_cents,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in existing
        ],
        "as_of": today.isoformat(),
    }


TOOL = Tool(
    name="lookup_order",
    description=(
        "Retrieve an order by its order number: status, delivery date, line "
        "items, total paid, and any refunds already issued against it. Use this "
        "first for any request that concerns a specific order. Returns facts "
        "only — it does not decide whether a refund is allowed."
    ),
    input_model=LookupOrderInput,
    handler=lookup_order,
    required_scopes=frozenset({"orders:read"}),
)
