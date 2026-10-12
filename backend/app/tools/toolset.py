"""Assembled toolsets and the scopes each agent role is granted."""

from __future__ import annotations

from app.tools import order_tools, policy_tools, refund_tools, restricted_tools
from app.tools.registry import ToolRegistry

# What the support agent may do. Note what is absent: finance:write.
SUPPORT_AGENT_SCOPES = frozenset({"orders:read", "policy:read", "refunds:create"})


def build_support_toolset() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(order_tools.TOOL)
    registry.register(policy_tools.TOOL)
    registry.register(refund_tools.TOOL)
    registry.register(restricted_tools.TOOL)
    return registry
