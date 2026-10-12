"""The model seam.

Nothing outside this module knows which provider is in use (DD-003), and the
agent loop depends only on the `ModelClient` protocol — which is what lets the
loop be tested without network access (DD-005).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.config import Settings, get_settings


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ThinkingBlock:
    """Present for provenance only. The loop never reads or edits its content."""


ContentBlock = TextBlock | ToolUseBlock | ThinkingBlock


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
        )


@dataclass(frozen=True)
class ModelResponse:
    stop_reason: str
    blocks: tuple[ContentBlock, ...]
    usage: Usage
    model: str
    # Exactly what to send back as the assistant turn. Passed through opaquely so
    # that provider-specific blocks — thinking signatures above all — survive the
    # round trip unmodified.
    assistant_content: Any = field(repr=False, default=None)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock)).strip()

    @property
    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolUseBlock))


@runtime_checkable
class ModelClient(Protocol):
    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse: ...


class AnthropicModelClient:
    """Calls the Anthropic Messages API.

    Model choice, effort, and token accounting live here rather than in the agent
    so that Phase 8 can put a routing gateway behind this same interface.
    """

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

    def credential_available(self) -> bool:
        """Whether a credential could be resolved at construction time.

        The SDK defers auth validation until the first request, which turns a
        missing key into a 500 deep inside a run. Checking up front lets the API
        answer with a clear 503 instead. Note the limitation: a credential that
        resolves lazily — an `ant auth login` profile, say — reads as unavailable
        here, so this gates the error message, never the request itself.
        """
        return bool(
            getattr(self._client, "api_key", None) or getattr(self._client, "auth_token", None)
        )

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
            tools=list(tools),
            thinking={"type": "adaptive"},
            output_config={"effort": self.settings.effort},
        )
        return ModelResponse(
            stop_reason=response.stop_reason or "end_turn",
            blocks=tuple(_normalize(block) for block in response.content),
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                cache_creation_input_tokens=getattr(
                    response.usage, "cache_creation_input_tokens", 0
                )
                or 0,
            ),
            model=response.model,
            assistant_content=response.content,
        )


def _normalize(block: Any) -> ContentBlock:
    match block.type:
        case "text":
            return TextBlock(text=block.text)
        case "tool_use":
            return ToolUseBlock(id=block.id, name=block.name, input=dict(block.input or {}))
        case _:
            # thinking, redacted_thinking, and anything a future model adds.
            return ThinkingBlock()
