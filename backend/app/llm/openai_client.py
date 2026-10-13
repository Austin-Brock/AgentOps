"""OpenAI adapter.

The translation work concentrated in this file is the reason the seam exists.
OpenAI differs from Anthropic in four ways that matter to an agent loop:

    system prompt   a message in the array, not a top-level field
    tool specs      nested under {"type": "function", "function": {...}}
    tool calls      hang off the assistant message, not inside its content
    tool results    one message per call, role="tool", carrying a tool_call_id

Arguments also arrive as a JSON *string* rather than a parsed object, so a model
that emits malformed JSON is a case this adapter has to handle rather than crash
on.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from openai import OpenAIError

from app.config import Settings, get_settings
from app.llm.client import (
    END_TURN,
    MAX_TOKENS,
    REFUSAL,
    TOOL_USE,
    ContentBlock,
    ModelResponse,
    TextBlock,
    ToolOutcome,
    ToolUseBlock,
    Usage,
)

_FINISH_REASONS = {
    "tool_calls": TOOL_USE,
    "function_call": TOOL_USE,
    "stop": END_TURN,
    "length": MAX_TOKENS,
    "content_filter": REFUSAL,
}


class OpenAIModelClient:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._init_error: Exception | None = None
        if client is not None:
            self._client = client
            return
        try:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()
        except OpenAIError as exc:
            # The OpenAI SDK raises at construction when no credential is
            # present; Anthropic defers until the first request. Swallowing it
            # here and surfacing it through `credential_available` means every
            # call site — the API's 503, preflight, the eval runner — behaves
            # identically no matter which provider is configured.
            self._client = None
            self._init_error = exc

    @property
    def model_name(self) -> str:
        return self.settings.model

    def credential_available(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "api_key", None))

    async def list_models(self, limit: int = 20) -> list[str]:
        if self._client is None:
            raise self._init_error or OpenAIError("OpenAI client is not configured.")
        page = await self._client.models.list()
        # The account-wide list includes embeddings, audio, and image models.
        # Only chat-capable families can serve an agent loop.
        chat = sorted(
            m.id for m in page.data if m.id.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-"))
        )
        return chat[:limit]

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        if self._client is None:
            raise self._init_error or OpenAIError("OpenAI client is not configured.")

        request: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": system}, *messages],
            # `max_completion_tokens`, not `max_tokens`: reasoning models reject
            # the older parameter outright.
            "max_completion_tokens": self.settings.max_tokens,
        }
        if tools:
            request["tools"] = [_tool_spec(spec) for spec in tools]
            request["tool_choice"] = "auto"
        if self.settings.reasoning_effort_supported:
            request["reasoning_effort"] = self.settings.effort

        completion = await self._client.chat.completions.create(**request)
        choice = completion.choices[0]
        message = choice.message

        blocks: list[ContentBlock] = []
        if message.content:
            blocks.append(TextBlock(text=message.content))
        for call in message.tool_calls or []:
            blocks.append(
                ToolUseBlock(
                    id=call.id,
                    name=call.function.name,
                    input=_parse_arguments(call.function.arguments),
                )
            )

        return ModelResponse(
            stop_reason=_FINISH_REASONS.get(choice.finish_reason or "stop", END_TURN),
            blocks=tuple(blocks),
            usage=_usage(completion.usage),
            model=completion.model,
            assistant_message=_assistant_message(message),
        )

    def tool_result_messages(self, outcomes: Sequence[ToolOutcome]) -> list[dict[str, Any]]:
        return tool_result_messages(outcomes)


def tool_result_messages(outcomes: Sequence[ToolOutcome]) -> list[dict[str, Any]]:
    """One message per result, each tagged with the call it answers.

    OpenAI has no `is_error` flag on a tool message, so a failure is carried in
    the payload itself — which it already is, since every tool error is a
    structured object with an `error` code.
    """
    return [
        {
            "role": "tool",
            "tool_call_id": outcome.tool_use_id,
            "content": json.dumps(outcome.payload, default=str),
        }
        for outcome in outcomes
    ]


def _tool_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["input_schema"],
        },
    }


def _assistant_message(message: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls
        ]
    return payload


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    """Tool arguments arrive as a JSON string and are not guaranteed to parse.

    Returning an empty dict on failure routes the problem through the executor's
    schema validation, which reports the missing fields back to the model as a
    recoverable error — the same path a merely-wrong argument takes.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        reasoning_tokens=getattr(completion_details, "reasoning_tokens", 0) or 0,
        cache_read_input_tokens=getattr(prompt_details, "cached_tokens", 0) or 0,
    )
