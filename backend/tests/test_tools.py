"""Tool behavior, exercised directly.

These assert the business rules that Phase 1 enforces in code rather than in the
prompt (DD-004). Each one is a rule the model must not be able to talk its way
around.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.data.store import DataStore
from app.tools import order_tools, policy_tools, refund_tools
from app.tools.base import ToolContext, ToolError
from app.tools.toolset import SUPPORT_AGENT_SCOPES


@pytest.fixture
def ctx(store: DataStore, settings: Settings) -> ToolContext:
    return ToolContext(store=store, settings=settings, granted_scopes=SUPPORT_AGENT_SCOPES)


class TestLookupOrder:
    async def test_returns_order_facts(self, ctx: ToolContext) -> None:
        result = await order_tools.lookup_order(order_tools.LookupOrderInput(order_id="4471"), ctx)
        assert result["order_id"] == "4471"
        assert result["status"] == "delivered"
        assert result["total_cents"] == 4299
        assert result["days_since_delivery"] == 6
        assert result["existing_refunds"] == []

    async def test_tolerates_hash_prefix(self, ctx: ToolContext) -> None:
        result = await order_tools.lookup_order(order_tools.LookupOrderInput(order_id="#4471"), ctx)
        assert result["order_id"] == "4471"

    async def test_unknown_order_is_a_tool_error(self, ctx: ToolContext) -> None:
        with pytest.raises(ToolError) as exc:
            await order_tools.lookup_order(order_tools.LookupOrderInput(order_id="9999"), ctx)
        assert exc.value.code == "order_not_found"

    async def test_surfaces_prior_refunds(self, ctx: ToolContext) -> None:
        result = await order_tools.lookup_order(order_tools.LookupOrderInput(order_id="4476"), ctx)
        assert [r["refund_id"] for r in result["existing_refunds"]] == ["RF-0001"]


class TestRefundPolicy:
    async def test_returns_the_policy_document(self, ctx: ToolContext) -> None:
        result = await policy_tools.get_refund_policy(policy_tools.GetRefundPolicyInput(), ctx)
        assert "30 days of the delivery date" in result["content"]
        assert "Approval thresholds" in result["content"]


class TestCreateRefundRequest:
    async def _create(self, ctx: ToolContext, **kwargs: object) -> dict:
        payload = {"order_id": "4471", "reason": "damaged_on_arrival", "amount_cents": 4299}
        payload.update(kwargs)
        return await refund_tools.create_refund_request(
            refund_tools.CreateRefundRequestInput(**payload),
            ctx,  # type: ignore[arg-type]
        )

    async def test_under_automatic_limit_is_approved(self, ctx: ToolContext) -> None:
        result = await self._create(ctx)
        assert result["status"] == refund_tools.AUTO_APPROVED
        assert result["refund_id"].startswith("RF-")
        assert ctx.store.refunds_for_order("4471")

    async def test_above_automatic_limit_requires_approval(self, ctx: ToolContext) -> None:
        result = await self._create(ctx, order_id="4475", amount_cents=7500)
        assert result["status"] == refund_tools.PENDING_MANAGER_APPROVAL
        assert "pending" in result["message"].lower()

    async def test_above_manager_limit_is_escalated(self, ctx: ToolContext) -> None:
        result = await self._create(ctx, order_id="4474", amount_cents=24999)
        assert result["status"] == refund_tools.ESCALATED_MANUAL_HANDLING

    async def test_outside_window_is_rejected(self, ctx: ToolContext) -> None:
        with pytest.raises(ToolError) as exc:
            await self._create(ctx, order_id="4472", amount_cents=2999)
        assert exc.value.code == "outside_refund_window"
        assert exc.value.details["days_since_delivery"] == 185
        assert not ctx.store.refunds_for_order("4472")

    async def test_undelivered_order_is_rejected(self, ctx: ToolContext) -> None:
        with pytest.raises(ToolError) as exc:
            await self._create(ctx, order_id="4473", amount_cents=15999)
        assert exc.value.code == "order_not_delivered"

    async def test_already_refunded_order_is_rejected(self, ctx: ToolContext) -> None:
        with pytest.raises(ToolError) as exc:
            await self._create(ctx, order_id="4476", amount_cents=1999, reason="defective")
        assert exc.value.code == "already_refunded"

    async def test_amount_above_order_total_is_rejected(self, ctx: ToolContext) -> None:
        with pytest.raises(ToolError) as exc:
            await self._create(ctx, amount_cents=99_999)
        assert exc.value.code == "amount_exceeds_order_total"
        assert not ctx.store.refunds_for_order("4471")

    async def test_unknown_order_is_rejected(self, ctx: ToolContext) -> None:
        with pytest.raises(ToolError) as exc:
            await self._create(ctx, order_id="9999")
        assert exc.value.code == "order_not_found"

    async def test_identical_request_is_deduplicated(self, ctx: ToolContext) -> None:
        first = await self._create(ctx)
        second = await self._create(ctx)
        assert second["deduplicated"] is True
        assert second["refund_id"] == first["refund_id"]
        assert len(ctx.store.refunds_for_order("4471")) == 1
