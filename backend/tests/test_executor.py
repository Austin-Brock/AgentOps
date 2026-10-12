"""Executor contract: authorize, then validate, then run.

Challenge 4 of Phase 1 lives here. The point is not that an unauthorized tool
fails — it is that it fails *before the handler is reachable*, so a denied call
can have no side effects.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.data.store import DataStore
from app.llm.client import ToolUseBlock
from app.observability.trace import RunTrace
from app.tools.base import Fault, FaultInjector, ToolContext
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.toolset import SUPPORT_AGENT_SCOPES


@pytest.fixture
def executor(registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(registry=registry, trace=RunTrace(run_id="test"))


@pytest.fixture
def ctx(store: DataStore, settings: Settings) -> ToolContext:
    return ToolContext(store=store, settings=settings, granted_scopes=SUPPORT_AGENT_SCOPES)


def use(name: str, **kwargs: object) -> ToolUseBlock:
    return ToolUseBlock(id=f"toolu_{name}", name=name, input=dict(kwargs))


class TestAuthorization:
    async def test_unauthorized_tool_is_blocked_before_the_handler_runs(
        self, executor: ToolExecutor, ctx: ToolContext
    ) -> None:
        # The handler for this tool raises if it is ever reached, so reaching it
        # would surface as tool_internal_error rather than permission_denied.
        invocation = await executor.execute(
            use("apply_account_credit", customer_id="C-1001", amount_cents=5000), ctx
        )
        assert invocation.executed is False
        assert invocation.result.code == "permission_denied"
        assert invocation.result.payload["missing_scopes"] == ["finance:write"]

    async def test_denial_tells_the_agent_not_to_retry(
        self, executor: ToolExecutor, ctx: ToolContext
    ) -> None:
        invocation = await executor.execute(
            use("apply_account_credit", customer_id="C-1001", amount_cents=100), ctx
        )
        assert "Do not retry" in invocation.result.payload["message"]

    async def test_granting_the_scope_reaches_the_handler(
        self, executor: ToolExecutor, ctx: ToolContext
    ) -> None:
        ctx.granted_scopes = SUPPORT_AGENT_SCOPES | {"finance:write"}
        invocation = await executor.execute(
            use("apply_account_credit", customer_id="C-1001", amount_cents=100), ctx
        )
        # Proof the block above was the permission check and not a missing handler.
        assert invocation.executed is True
        assert invocation.result.code == "tool_internal_error"

    async def test_authorized_tool_succeeds(self, executor: ToolExecutor, ctx: ToolContext) -> None:
        invocation = await executor.execute(use("lookup_order", order_id="4471"), ctx)
        assert invocation.executed is True
        assert invocation.is_error is False
        assert invocation.result.payload["order_id"] == "4471"


class TestValidation:
    async def test_unknown_tool_lists_what_is_available(
        self, executor: ToolExecutor, ctx: ToolContext
    ) -> None:
        invocation = await executor.execute(use("refund_everything"), ctx)
        assert invocation.result.code == "unknown_tool"
        assert "lookup_order" in invocation.result.payload["message"]

    async def test_schema_violation_names_the_offending_field(
        self, executor: ToolExecutor, ctx: ToolContext
    ) -> None:
        invocation = await executor.execute(
            use(
                "create_refund_request",
                order_id="4471",
                reason="damaged_on_arrival",
                amount_cents=0,
            ),
            ctx,
        )
        assert invocation.executed is False
        assert invocation.result.code == "invalid_input"
        assert invocation.result.payload["violations"][0]["field"] == "amount_cents"
        assert not ctx.store.refunds_for_order("4471")

    async def test_invalid_enum_value_is_rejected(
        self, executor: ToolExecutor, ctx: ToolContext
    ) -> None:
        invocation = await executor.execute(
            use(
                "create_refund_request",
                order_id="4471",
                reason="because_i_said_so",
                amount_cents=100,
            ),
            ctx,
        )
        assert invocation.result.code == "invalid_input"


class TestFailureModes:
    async def test_business_failure_becomes_an_error_result(
        self, executor: ToolExecutor, ctx: ToolContext
    ) -> None:
        invocation = await executor.execute(use("lookup_order", order_id="9999"), ctx)
        assert invocation.executed is True
        assert invocation.is_error is True
        assert invocation.result.code == "order_not_found"

    async def test_injected_fault_fires_once_then_clears(
        self, executor: ToolExecutor, ctx: ToolContext
    ) -> None:
        ctx.faults = FaultInjector([Fault(tool="lookup_order", times=1)])
        first = await executor.execute(use("lookup_order", order_id="4471"), ctx)
        second = await executor.execute(use("lookup_order", order_id="4471"), ctx)
        assert first.result.code == "upstream_timeout"
        assert first.result.payload["retryable"] is True
        assert second.is_error is False

    async def test_error_results_are_serializable_for_the_model(
        self, executor: ToolExecutor, ctx: ToolContext
    ) -> None:
        invocation = await executor.execute(use("lookup_order", order_id="9999"), ctx)
        param = invocation.to_tool_result_param()
        assert param["type"] == "tool_result"
        assert param["is_error"] is True
        assert param["tool_use_id"] == "toolu_lookup_order"
        assert "order_not_found" in param["content"]


class TestVisibility:
    def test_unauthorized_tools_are_advertised_by_default(
        self, registry: ToolRegistry, ctx: ToolContext
    ) -> None:
        # Discovery is not authorization: the agent can see the finance tool and
        # still cannot run it. Phase 4 has to defend the same boundary over MCP.
        names = [spec["name"] for spec in registry.specs(ctx)]
        assert "apply_account_credit" in names

    def test_visibility_can_be_narrowed_to_authorized_tools(
        self, registry: ToolRegistry, ctx: ToolContext
    ) -> None:
        ctx.settings = ctx.settings.model_copy(update={"expose_unauthorized_tools": False})
        names = [spec["name"] for spec in registry.specs(ctx)]
        assert "apply_account_credit" not in names
        assert "lookup_order" in names

    def test_specs_carry_a_closed_input_schema(self, registry: ToolRegistry) -> None:
        spec = registry.tools["create_refund_request"].spec()
        assert spec["input_schema"]["additionalProperties"] is False
        assert "amount_cents" in spec["input_schema"]["properties"]
