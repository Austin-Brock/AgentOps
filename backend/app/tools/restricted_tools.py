"""A tool the support agent is advertised but not authorized to run.

Challenge 4 of Phase 1 needs a real permission boundary to test, not a missing
function. This tool is registered, described, and visible to the model; the
executor refuses it because the support agent is not granted `finance:write`.

The handler deliberately raises instead of doing the work. If the permission
check ever regresses, every test and scenario that touches this tool fails
loudly rather than quietly moving money.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.tools.base import Tool, ToolContext


class ApplyAccountCreditInput(BaseModel):
    customer_id: str = Field(description="The customer receiving the credit.")
    amount_cents: int = Field(gt=0, description="Credit amount in cents.")
    note: str | None = Field(default=None, description="Why the credit is being applied.")


async def apply_account_credit(args: ApplyAccountCreditInput, ctx: ToolContext) -> dict[str, Any]:
    raise AssertionError(
        "apply_account_credit handler executed: the permission check did not run. "
        "This tool requires the finance:write scope and the support agent does not "
        "hold it."
    )


TOOL = Tool(
    name="apply_account_credit",
    description=(
        "Apply store credit directly to a customer's account balance, bypassing "
        "the refund workflow. Restricted to the finance team."
    ),
    input_model=ApplyAccountCreditInput,
    handler=apply_account_credit,
    required_scopes=frozenset({"finance:write"}),
    destructive=True,
)
