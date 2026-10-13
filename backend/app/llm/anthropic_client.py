"""Anthropic adapter."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from app.config import Settings, get_settings
from app.llm.client import (
    END_TURN,
    ContentBlock,
    ModelResponse,
    ReasoningBlock,
    TextBlock,
    ToolOutcome,
    ToolUseBlock,
    Usage,
)

# Anthropic's stop reasons already match the normalized vocabulary, except for
# the ones the loop does not distinguish.
_STOP_REASONS = {
    "tool_use": "tool_use",
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "stop_sequence": "end_turn",
    "pause_turn": "tool_use",
}


class AnthropicModelClient:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()

    @property
    def model_name(self) -> str:
        return self.settings.model

    def credential_available(self) -> bool:
        return bool(
            getattr(self._client, "api_key", None) or getattr(self._client, "auth_token", None)
        )

    async def list_models(self, limit: int = 20) -> list[str]:
        page = await self._client.models.list(limit=limit)
        return [model.id for model in page.data]

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        response = await self._client.messages.create(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=system,
            messages=messages,
            tools=[dict(spec) for spec in tools],
            thinking={"type": "adaptive"},
            output_config={"effort": self.settings.effort},
        )
        usage = response.usage
        return ModelResponse(
            stop_reason=_STOP_REASONS.get(response.stop_reason or END_TURN, END_TURN),
            blocks=tuple(_normalize(block) for block in response.content),
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            ),
            model=response.model,
            # Content blocks are passed back exactly as received so that thinking
            # signatures survive the round trip unmodified.
            assistant_message={"role": "assistant", "content": response.content},
        )

    def tool_result_messages(self, outcomes: Sequence[ToolOutcome]) -> list[dict[str, Any]]:
        return tool_result_messages(outcomes)


def tool_result_messages(outcomes: Sequence[ToolOutcome]) -> list[dict[str, Any]]:
    """One user message carrying every result.

    Splitting results across messages teaches the model to stop requesting tools
    in parallel, so they must arrive together.
    """
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": outcome.tool_use_id,
                    "content": json.dumps(outcome.payload, default=str),
                    "is_error": outcome.is_error,
                }
                for outcome in outcomes
            ],
        }
    ]


def _normalize(block: Any) -> ContentBlock:
    match block.type:
        case "text":
            return TextBlock(text=block.text)
        case "tool_use":
            return ToolUseBlock(id=block.id, name=block.name, input=dict(block.input or {}))
        case _:
            return ReasoningBlock()
