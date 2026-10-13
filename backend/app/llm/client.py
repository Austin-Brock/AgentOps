"""The provider seam.

An earlier version of this file claimed nothing outside it knew which provider
was in use. That was not true: the agent loop built tool-result messages in
Anthropic's wire format, and the executor stamped Anthropic's `tool_result`
block type. Both leaked.

The protocol below fixes that by making message *construction* a provider
responsibility, not the loop's. Providers disagree about more than field names:

    Anthropic  one user message carrying N tool_result blocks
    OpenAI     N separate messages, role="tool", one tool_call_id each

A loop that hardcodes either shape is hardcoding a provider. So the loop asks
the client to format both the assistant turn and the tool results, and never
inspects either.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ReasoningBlock:
    """A provider's internal reasoning. Carried for provenance; never inspected."""


ContentBlock = TextBlock | ToolUseBlock | ReasoningBlock


@dataclass(frozen=True)
class ToolOutcome:
    """A executed tool call, in provider-neutral terms.

    Deliberately not `ToolInvocation` — that type belongs to the tool layer, and
    importing it here would make the seam depend on what it is a seam for.
    """

    tool_use_id: str
    tool_name: str
    payload: dict[str, Any]
    is_error: bool


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
        )


# Normalized stop reasons. Every adapter maps its provider's vocabulary onto
# these, so the loop's termination logic is written once.
TOOL_USE = "tool_use"
END_TURN = "end_turn"
MAX_TOKENS = "max_tokens"
REFUSAL = "refusal"


@dataclass(frozen=True)
class ModelResponse:
    stop_reason: str
    blocks: tuple[ContentBlock, ...]
    usage: Usage
    model: str
    # The complete assistant message to append to history. Opaque: OpenAI hangs
    # tool calls off the message, Anthropic puts them in content, and the loop
    # must not care which.
    assistant_message: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock)).strip()

    @property
    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolUseBlock))


@runtime_checkable
class ModelClient(Protocol):
    """What the agent loop requires of a model provider.

    `tools` are neutral specs — `{"name", "description", "input_schema"}` — and
    each adapter translates them into its provider's tool format.
    """

    @property
    def model_name(self) -> str: ...

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse: ...

    def tool_result_messages(self, outcomes: Sequence[ToolOutcome]) -> list[dict[str, Any]]: ...
