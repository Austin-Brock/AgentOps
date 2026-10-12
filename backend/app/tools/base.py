"""Tool primitives: declaration, context, results, and failure modes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.config import Settings, get_settings
from app.data.store import DataStore


class ToolError(Exception):
    """A business-level failure the model is expected to read and react to.

    Tool failures are data, not exceptions to be swallowed: the loop turns them
    into an error tool_result so the agent can change its approach.
    """

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.details}


@dataclass
class ToolContext:
    """Everything a tool may touch, plus the authority it is being run under."""

    store: DataStore
    settings: Settings = field(default_factory=get_settings)
    customer_id: str | None = None
    granted_scopes: frozenset[str] = frozenset()
    faults: FaultInjector | None = None


@dataclass(frozen=True)
class ToolResult:
    payload: dict[str, Any]
    is_error: bool = False
    code: str | None = None


ToolHandler = Callable[[Any, ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    required_scopes: frozenset[str] = frozenset()
    destructive: bool = False

    def spec(self) -> dict[str, Any]:
        """The tool definition sent to the model."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": json_schema_for(self.input_model),
        }


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for a tool's inputs, trimmed of noise the model does not need."""
    schema = model.model_json_schema()
    schema = _strip_titles(schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema["additionalProperties"] = False
    return schema


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


# --- Fault injection -------------------------------------------------------


@dataclass
class Fault:
    """A tool failure scheduled in advance, so recovery can be tested."""

    tool: str
    code: str = "upstream_timeout"
    message: str = "The upstream service did not respond in time."
    times: int = 1
    fired: int = 0

    @property
    def spent(self) -> bool:
        return self.fired >= self.times


@dataclass
class FaultInjector:
    faults: list[Fault] = field(default_factory=list)

    def maybe_raise(self, tool_name: str) -> None:
        for fault in self.faults:
            if fault.tool == tool_name and not fault.spent:
                fault.fired += 1
                raise ToolError(fault.code, fault.message, retryable=True)
