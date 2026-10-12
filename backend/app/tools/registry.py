"""Tool registry.

Holds the tools an agent can see. Visibility and authorization are separate
concerns here on purpose: the registry decides what is advertised, the executor
decides what may run.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from app.tools.base import Tool, ToolContext


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self.tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self.tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self.tools.values())

    def __len__(self) -> int:
        return len(self.tools)

    def specs(self, ctx: ToolContext) -> list[dict[str, Any]]:
        """Tool definitions to advertise to the model.

        When `expose_unauthorized_tools` is on — the default — tools the agent
        lacks scopes for are still advertised. That is deliberate: it is the only
        way to prove end to end that discovering a tool does not grant the right
        to execute it, which is the same boundary Phase 4 has to defend across
        MCP.
        """
        if ctx.settings.expose_unauthorized_tools:
            return [tool.spec() for tool in self.tools.values()]
        return [
            tool.spec()
            for tool in self.tools.values()
            if tool.required_scopes <= ctx.granted_scopes
        ]
