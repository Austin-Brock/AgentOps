"""Tool execution: authorize, validate, run, and turn failures into data.

The order of the checks below is the contract. Authorization is decided before
arguments are even parsed, and well before any handler runs, so a denied call
cannot have side effects.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.llm.client import ToolUseBlock
from app.observability.trace import RunTrace
from app.tools.base import ToolContext, ToolError, ToolResult
from app.tools.registry import ToolRegistry


@dataclass(frozen=True)
class ToolInvocation:
    tool_use_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: ToolResult
    executed: bool
    duration_ms: float = 0.0

    @property
    def is_error(self) -> bool:
        return self.result.is_error

    def to_tool_result_param(self) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": json.dumps(self.result.payload, default=str),
            "is_error": self.result.is_error,
        }


@dataclass
class ToolExecutor:
    registry: ToolRegistry
    trace: RunTrace | None = field(default=None)

    async def execute(self, tool_use: ToolUseBlock, ctx: ToolContext) -> ToolInvocation:
        tool = self.registry.get(tool_use.name)

        # 1. Does the tool exist? A hallucinated tool name is the model's error
        #    to recover from, not ours to crash on.
        if tool is None:
            return self._refuse(
                tool_use,
                code="unknown_tool",
                message=(
                    f"No tool named {tool_use.name!r}. Available tools: "
                    + ", ".join(sorted(self.registry.tools))
                ),
            )

        # 2. Is the agent authorized? Checked before parsing arguments and before
        #    the handler is reachable, so a denial can have no side effects.
        if not tool.required_scopes <= ctx.granted_scopes:
            missing = sorted(tool.required_scopes - ctx.granted_scopes)
            return self._refuse(
                tool_use,
                code="permission_denied",
                message=(
                    f"Not authorized to execute {tool.name!r}: missing scope(s) "
                    f"{', '.join(missing)}. Do not retry this tool; find another "
                    "way to help the customer or escalate."
                ),
                missing_scopes=missing,
            )

        # 3. Do the arguments satisfy the schema? Invalid input is recoverable —
        #    the model usually fixes it on the next turn when told what was wrong.
        try:
            args = tool.input_model.model_validate(tool_use.input)
        except ValidationError as exc:
            return self._refuse(
                tool_use,
                code="invalid_input",
                message=f"Arguments did not match the schema for {tool.name!r}.",
                violations=[
                    {"field": ".".join(str(p) for p in err["loc"]), "problem": err["msg"]}
                    for err in exc.errors()
                ],
            )

        started = time.perf_counter()
        try:
            # 4. Injected faults stand in for real upstream failure.
            if ctx.faults is not None:
                ctx.faults.maybe_raise(tool.name)
            payload = await tool.handler(args, ctx)
        except ToolError as exc:
            return self._finish(
                tool_use,
                args.model_dump(),
                ToolResult(payload=exc.to_payload(), is_error=True, code=exc.code),
                executed=True,
                started=started,
            )
        except Exception as exc:  # noqa: BLE001 - an unexpected bug is still the agent's problem
            return self._finish(
                tool_use,
                args.model_dump(),
                ToolResult(
                    payload={
                        "error": "tool_internal_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                    is_error=True,
                    code="tool_internal_error",
                ),
                executed=True,
                started=started,
            )

        return self._finish(
            tool_use,
            args.model_dump(),
            ToolResult(payload=payload),
            executed=True,
            started=started,
        )

    # --- helpers ---

    def _refuse(
        self, tool_use: ToolUseBlock, *, code: str, message: str, **details: Any
    ) -> ToolInvocation:
        invocation = ToolInvocation(
            tool_use_id=tool_use.id,
            tool_name=tool_use.name,
            arguments=dict(tool_use.input),
            result=ToolResult(
                payload={"error": code, "message": message, **details},
                is_error=True,
                code=code,
            ),
            executed=False,
        )
        if self.trace:
            self.trace.record("tool_refused", tool=tool_use.name, reason=code)
        return invocation

    def _finish(
        self,
        tool_use: ToolUseBlock,
        arguments: dict[str, Any],
        result: ToolResult,
        *,
        executed: bool,
        started: float,
    ) -> ToolInvocation:
        duration_ms = (time.perf_counter() - started) * 1000
        if self.trace:
            self.trace.record(
                "tool_failed" if result.is_error else "tool_succeeded",
                tool=tool_use.name,
                **({"reason": result.code} if result.is_error else {}),
            )
        return ToolInvocation(
            tool_use_id=tool_use.id,
            tool_name=tool_use.name,
            arguments=arguments,
            result=result,
            executed=executed,
            duration_ms=duration_ms,
        )
