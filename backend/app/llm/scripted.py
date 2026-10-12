"""A scripted model client for deterministic tests.

The agent loop's mechanics — termination, step ceilings, tool-error handling,
permission denial, message threading — are ordinary logic and deserve ordinary
tests. Paying for tokens to assert that a step counter increments is a bad
trade, and a suite that needs credentials is a suite that does not run (DD-005).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.llm.client import ModelResponse, TextBlock, ToolUseBlock, Usage


def say(text: str, *, stop_reason: str = "end_turn") -> ModelResponse:
    """A final text answer."""
    return ModelResponse(
        stop_reason=stop_reason,
        blocks=(TextBlock(text=text),),
        usage=Usage(input_tokens=10, output_tokens=5),
        model="scripted",
        assistant_content=[{"type": "text", "text": text}],
    )


def call(
    name: str,
    tool_input: dict[str, Any] | None = None,
    *,
    text: str | None = None,
    call_id: str | None = None,
) -> ModelResponse:
    """A turn that requests one tool, optionally with preamble text."""
    return calls([(name, tool_input or {}, call_id)], text=text)


def calls(
    requested: Sequence[tuple[str, dict[str, Any], str | None]],
    *,
    text: str | None = None,
) -> ModelResponse:
    """A turn that requests several tools at once (parallel tool use)."""
    blocks: list[Any] = []
    content: list[dict[str, Any]] = []
    if text:
        blocks.append(TextBlock(text=text))
        content.append({"type": "text", "text": text})
    for index, (name, tool_input, call_id) in enumerate(requested):
        resolved_id = call_id or f"toolu_scripted_{name}_{index}"
        blocks.append(ToolUseBlock(id=resolved_id, name=name, input=tool_input))
        content.append({"type": "tool_use", "id": resolved_id, "name": name, "input": tool_input})
    return ModelResponse(
        stop_reason="tool_use",
        blocks=tuple(blocks),
        usage=Usage(input_tokens=10, output_tokens=5),
        model="scripted",
        assistant_content=content,
    )


class ScriptedModelClient:
    """Replays a fixed list of responses, recording what it was asked.

    Running past the end of the script raises rather than looping forever — an
    exhausted script means the agent took a path the test did not anticipate,
    which is a result worth seeing.
    """

    def __init__(self, responses: Sequence[ModelResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def exhausted(self) -> bool:
        return not self._responses

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        self.calls.append(
            {
                "system": system,
                "messages": [dict(m) for m in messages],
                "tool_names": [t["name"] for t in tools],
            }
        )
        if not self._responses:
            raise AssertionError(
                f"ScriptedModelClient exhausted after {len(self.calls)} call(s): "
                "the agent requested another turn the script did not provide."
            )
        return self._responses.pop(0)


class LoopingModelClient:
    """Always asks for the same tool. Used to prove the step ceiling holds."""

    def __init__(self, tool_name: str, tool_input: dict[str, Any] | None = None) -> None:
        self.tool_name = tool_name
        self.tool_input = tool_input or {}
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        self.calls.append({"messages": [dict(m) for m in messages]})
        return call(self.tool_name, self.tool_input, call_id=f"toolu_loop_{len(self.calls)}")
