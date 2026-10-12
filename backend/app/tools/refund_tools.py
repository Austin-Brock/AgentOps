"""Refund creation.

This is where DD-004 earns its place. The agent decides *that* a refund should
be created; this module decides whether it may be, and at what approval level.
Every rule below is re-checked here regardless of what the model concluded from
the policy document, because a prompt is a request and a check is a constraint.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.tools.base import Tool, ToolContext, ToolError

RefundReason = Literal[
    "damaged_on_arrival",
    "defective",
    "not_as_described",
    "wrong_item",
    "changed_mind",
    "other",
]

AUTO_APPROVED = "auto_approved"
PENDING_MANAGER_APPROVAL = "pending_manager_approval"
ESCALATED_MANUAL_HANDLING = "escalated_manual_handling"


class CreateRefundRequestInput(BaseModel):
    order_id: str = Field(description="The order number the refund applies to.")
    reason: RefundReason = Field(
        description="Why the customer is asking for a refund, in policy terms."
    )
    amount_cents: int = Field(
        gt=0,
        description=(
            "Refund amount in cents. May not exceed the amount paid for the "
            "order. Use the order total for a full refund."
        ),
    )
    customer_note: str | None = Field(
        default=None,
        description="A short quote or summary of what the customer reported.",
    )


async def create_refund_request(args: CreateRefundRequestInput, ctx: ToolContext) -> dict[str, Any]:
    settings = ctx.settings
    store = ctx.store

    order = store.get_order(args.order_id)
    if order is None:
        raise ToolError(
            "order_not_found",
            f"No order matches {args.order_id!r}. A refund cannot be created.",
            order_id=args.order_id,
        )

    # Idempotency: the same refund requested twice is one refund. A sketch of the
    # real mechanism, which Phase 9 builds out with durable execution.
    idempotency_key = f"{order.order_id}:{args.reason}:{args.amount_cents}"
    if existing := store.find_refund_by_idempotency_key(idempotency_key):
        return {
            "refund_id": existing.refund_id,
            "order_id": existing.order_id,
            "amount_cents": existing.amount_cents,
            "status": existing.status,
            "created_at": existing.created_at,
            "deduplicated": True,
            "message": "An identical refund request already exists; returning it unchanged.",
        }

    if order.delivered_at is None:
        raise ToolError(
            "order_not_delivered",
            (
                f"Order {order.order_id} has status {order.status!r} and has not been "
                "delivered. Undelivered orders are cancelled, not refunded."
            ),
            order_id=order.order_id,
            order_status=order.status,
        )

    days = order.days_since_delivery(settings.simulated_today)
    assert days is not None
    if days > settings.refund_window_days:
        raise ToolError(
            "outside_refund_window",
            (
                f"Order {order.order_id} was delivered {days} days ago, outside the "
                f"{settings.refund_window_days}-day refund window. A refund cannot be "
                "created. A warranty claim may still apply."
            ),
            order_id=order.order_id,
            days_since_delivery=days,
            window_days=settings.refund_window_days,
        )

    if prior := store.refunds_for_order(order.order_id):
        raise ToolError(
            "already_refunded",
            (
                f"Order {order.order_id} has already been refunded "
                f"({prior[0].refund_id}). A second refund cannot be created; this is "
                "a billing dispute and is escalated for manual handling."
            ),
            order_id=order.order_id,
            existing_refund_id=prior[0].refund_id,
        )

    if args.amount_cents > order.total_cents:
        raise ToolError(
            "amount_exceeds_order_total",
            (
                f"Requested {args.amount_cents} cents but order {order.order_id} totals "
                f"{order.total_cents} cents. A refund may never exceed the amount paid."
            ),
            order_id=order.order_id,
            requested_cents=args.amount_cents,
            order_total_cents=order.total_cents,
        )

    status = _approval_status(args.amount_cents, ctx)
    refund = store.create_refund(
        order_id=order.order_id,
        customer_id=order.customer_id,
        amount_cents=args.amount_cents,
        reason=args.reason,
        status=status,
        idempotency_key=idempotency_key,
    )

    return {
        "refund_id": refund.refund_id,
        "order_id": refund.order_id,
        "amount_cents": refund.amount_cents,
        "reason": refund.reason,
        "status": status,
        "created_at": refund.created_at,
        "customer_note": args.customer_note,
        "message": _status_message(status, args.amount_cents, ctx),
    }


def _approval_status(amount_cents: int, ctx: ToolContext) -> str:
    settings = ctx.settings
    if amount_cents <= settings.refund_auto_approve_max_cents:
        return AUTO_APPROVED
    if amount_cents <= settings.refund_manager_approval_max_cents:
        return PENDING_MANAGER_APPROVAL
    return ESCALATED_MANUAL_HANDLING


def _status_message(status: str, amount_cents: int, ctx: ToolContext) -> str:
    dollars = amount_cents / 100
    match status:
        case s if s == AUTO_APPROVED:
            return (
                f"Refund of ${dollars:,.2f} is approved and will be released to the "
                "original payment method."
            )
        case s if s == PENDING_MANAGER_APPROVAL:
            return (
                f"Refund of ${dollars:,.2f} is above the automatic limit of "
                f"${ctx.settings.refund_auto_approve_max_cents / 100:,.2f} and is queued "
                "for manager approval. It is not yet released. Tell the customer it is "
                "pending review — do not tell them the money is on its way."
            )
        case _:
            return (
                f"Refund of ${dollars:,.2f} is above "
                f"${ctx.settings.refund_manager_approval_max_cents / 100:,.2f} and has been "
                "escalated for manual handling by the resolutions team. It is not "
                "approved. Tell the customer a specialist will follow up."
            )


TOOL = Tool(
    name="create_refund_request",
    description=(
        "Create a refund request against an order. The system independently "
        "verifies the order exists, was delivered, is inside the refund window, "
        "has not already been refunded, and that the amount does not exceed what "
        "was paid — a request violating any of these is rejected. Amounts above "
        "the automatic limit are created in a pending or escalated state rather "
        "than released, so read the returned status before telling the customer "
        "what will happen."
    ),
    input_model=CreateRefundRequestInput,
    handler=create_refund_request,
    required_scopes=frozenset({"refunds:create"}),
    destructive=True,
)
