"""Provider adapters and the neutrality of the seam.

Two things are asserted here. First, that each adapter correctly translates
between the neutral types and its provider's wire format — tested against fake
SDK clients, so no network and no credentials. Second, and more importantly,
that the *agent loop* produces provider-correct message threading without
knowing which provider it is talking to. That second property is the one an
earlier version of this code claimed and did not have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

from app.agents.support_agent import SupportAgent
from app.config import Settings
from app.llm import anthropic_client, openai_client
from app.llm.client import END_TURN, MAX_TOKENS, REFUSAL, TOOL_USE, ToolOutcome
from app.llm.factory import build_model_client, credential_available
from app.llm.openai_client import OpenAIModelClient
from app.llm.scripted import ScriptedModelClient, call, say

# --- fake OpenAI SDK objects ------------------------------------------------


@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] = field(default_factory=list)


@dataclass
class FakeChoice:
    message: FakeMessage
    finish_reason: str = "stop"


@dataclass
class FakeCompletionDetails:
    reasoning_tokens: int = 0


@dataclass
class FakePromptDetails:
    cached_tokens: int = 0


@dataclass
class FakeUsage:
    prompt_tokens: int = 100
    completion_tokens: int = 40
    completion_tokens_details: FakeCompletionDetails = field(default_factory=FakeCompletionDetails)
    prompt_tokens_details: FakePromptDetails = field(default_factory=FakePromptDetails)


@dataclass
class FakeCompletion:
    choices: list[FakeChoice]
    model: str = "gpt-test"
    usage: FakeUsage = field(default_factory=FakeUsage)


class FakeCompletions:
    def __init__(self, completion: FakeCompletion) -> None:
        self._completion = completion
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeCompletion:
        self.requests.append(kwargs)
        return self._completion


class FakeOpenAI:
    api_key = "sk-test"

    def __init__(self, completion: FakeCompletion) -> None:
        self.chat = type("Chat", (), {"completions": FakeCompletions(completion)})()

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.chat.completions.requests


def openai_adapter(completion: FakeCompletion, **overrides: Any) -> OpenAIModelClient:
    settings = Settings(provider="openai", model="gpt-test", **overrides)
    return OpenAIModelClient(settings=settings, client=FakeOpenAI(completion))


# --- translation ------------------------------------------------------------


class TestOpenAIRequestTranslation:
    async def test_system_prompt_becomes_the_first_message(self) -> None:
        adapter = openai_adapter(FakeCompletion([FakeChoice(FakeMessage(content="hi"))]))
        await adapter.create(
            system="You are a support agent.",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )
        sent = adapter._client.requests[0]["messages"]
        assert sent[0] == {"role": "system", "content": "You are a support agent."}
        assert sent[1]["content"] == "hello"

    async def test_tool_specs_are_nested_under_function(self) -> None:
        adapter = openai_adapter(FakeCompletion([FakeChoice(FakeMessage(content="hi"))]))
        await adapter.create(
            system="s",
            messages=[],
            tools=[
                {
                    "name": "lookup_order",
                    "description": "Find an order.",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        )
        tool = adapter._client.requests[0]["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "lookup_order"
        assert tool["function"]["parameters"] == {"type": "object", "properties": {}}

    async def test_uses_max_completion_tokens_not_max_tokens(self) -> None:
        """Reasoning models reject `max_tokens` outright."""
        adapter = openai_adapter(FakeCompletion([FakeChoice(FakeMessage(content="hi"))]))
        await adapter.create(system="s", messages=[], tools=[])
        request = adapter._client.requests[0]
        assert "max_completion_tokens" in request
        assert "max_tokens" not in request

    async def test_reasoning_effort_can_be_suppressed_per_model(self) -> None:
        adapter = openai_adapter(
            FakeCompletion([FakeChoice(FakeMessage(content="hi"))]),
            reasoning_effort_supported=False,
        )
        await adapter.create(system="s", messages=[], tools=[])
        assert "reasoning_effort" not in adapter._client.requests[0]


class TestOpenAIResponseTranslation:
    async def test_tool_calls_become_tool_use_blocks(self) -> None:
        completion = FakeCompletion(
            [
                FakeChoice(
                    FakeMessage(
                        content="Checking that order.",
                        tool_calls=[
                            FakeToolCall(
                                id="call_abc",
                                function=FakeFunction(
                                    name="lookup_order", arguments='{"order_id": "4471"}'
                                ),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )
        response = await openai_adapter(completion).create(system="s", messages=[], tools=[])

        assert response.stop_reason == TOOL_USE
        assert response.text == "Checking that order."
        assert len(response.tool_uses) == 1
        assert response.tool_uses[0].id == "call_abc"
        assert response.tool_uses[0].input == {"order_id": "4471"}

    async def test_malformed_arguments_do_not_crash_the_run(self) -> None:
        """A model that emits broken JSON is routed through schema validation."""
        completion = FakeCompletion(
            [
                FakeChoice(
                    FakeMessage(
                        tool_calls=[
                            FakeToolCall(
                                id="call_bad",
                                function=FakeFunction(
                                    name="lookup_order", arguments='{"order_id": '
                                ),
                            )
                        ]
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )
        response = await openai_adapter(completion).create(system="s", messages=[], tools=[])
        assert response.tool_uses[0].input == {}

    @pytest.mark.parametrize(
        ("finish_reason", "expected"),
        [
            ("stop", END_TURN),
            ("tool_calls", TOOL_USE),
            ("length", MAX_TOKENS),
            ("content_filter", REFUSAL),
            ("something_new", END_TURN),
        ],
    )
    async def test_finish_reasons_map_onto_the_normalized_vocabulary(
        self, finish_reason: str, expected: str
    ) -> None:
        completion = FakeCompletion(
            [FakeChoice(FakeMessage(content="x"), finish_reason=finish_reason)]
        )
        response = await openai_adapter(completion).create(system="s", messages=[], tools=[])
        assert response.stop_reason == expected

    async def test_usage_includes_reasoning_and_cached_tokens(self) -> None:
        completion = FakeCompletion(
            [FakeChoice(FakeMessage(content="x"))],
            usage=FakeUsage(
                prompt_tokens=500,
                completion_tokens=120,
                completion_tokens_details=FakeCompletionDetails(reasoning_tokens=90),
                prompt_tokens_details=FakePromptDetails(cached_tokens=256),
            ),
        )
        response = await openai_adapter(completion).create(system="s", messages=[], tools=[])
        assert response.usage.input_tokens == 500
        assert response.usage.output_tokens == 120
        assert response.usage.reasoning_tokens == 90
        assert response.usage.cache_read_input_tokens == 256

    async def test_assistant_message_carries_tool_calls_at_message_level(self) -> None:
        completion = FakeCompletion(
            [
                FakeChoice(
                    FakeMessage(
                        content=None,
                        tool_calls=[
                            FakeToolCall(
                                id="call_1",
                                function=FakeFunction(name="get_refund_policy", arguments="{}"),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )
        response = await openai_adapter(completion).create(system="s", messages=[], tools=[])
        assert response.assistant_message["tool_calls"][0]["id"] == "call_1"
        assert response.assistant_message["tool_calls"][0]["type"] == "function"


# --- the two wire formats differ, and the loop must not care ----------------


OUTCOMES = (
    ToolOutcome("id-1", "lookup_order", {"order_id": "4471"}, is_error=False),
    ToolOutcome("id-2", "get_refund_policy", {"error": "boom"}, is_error=True),
)


class TestToolResultFormatting:
    def test_anthropic_returns_one_message_with_every_result(self) -> None:
        messages = anthropic_client.tool_result_messages(OUTCOMES)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        blocks = messages[0]["content"]
        assert [b["tool_use_id"] for b in blocks] == ["id-1", "id-2"]
        assert blocks[1]["is_error"] is True

    def test_openai_returns_one_message_per_result(self) -> None:
        messages = openai_client.tool_result_messages(OUTCOMES)
        assert len(messages) == 2
        assert {m["role"] for m in messages} == {"tool"}
        assert [m["tool_call_id"] for m in messages] == ["id-1", "id-2"]
        # No is_error flag exists; the failure rides in the payload.
        assert "error" in messages[1]["content"]


class TestLoopIsProviderNeutral:
    """The same scripted run, threaded through both wire formats."""

    SCRIPT: ClassVar = (
        call("lookup_order", {"order_id": "4471"}, call_id="c1"),
        say("Order 4471 was delivered on 2026-10-06."),
    )

    async def _run(self, wire_format: str, settings: Settings, store):
        client = ScriptedModelClient(self.SCRIPT, wire_format=wire_format)
        agent = SupportAgent(model=client, store=store, settings=settings)
        run = await agent.run("Where is order #4471?")
        return client, run

    async def test_anthropic_threading(self, settings: Settings, store) -> None:
        client, run = await self._run("anthropic", settings, store)
        results_turn = client.calls[1]["messages"][2]
        assert results_turn["role"] == "user"
        assert results_turn["content"][0]["type"] == "tool_result"
        assert run.status == "completed"

    async def test_openai_threading(self, settings: Settings, store) -> None:
        client, run = await self._run("openai", settings, store)
        results_turn = client.calls[1]["messages"][2]
        assert results_turn["role"] == "tool"
        assert results_turn["tool_call_id"] == "c1"
        assert run.status == "completed"

    async def test_both_formats_produce_identical_agent_behavior(
        self, settings: Settings, store
    ) -> None:
        _, anthropic_run = await self._run("anthropic", settings, store)
        store.refunds.clear()
        _, openai_run = await self._run("openai", settings, store)

        assert anthropic_run.tools_executed == openai_run.tools_executed
        assert anthropic_run.status == openai_run.status
        assert anthropic_run.final_text == openai_run.final_text


class TestFactory:
    def test_builds_the_configured_adapter(self) -> None:
        anthropic = build_model_client(Settings(provider="anthropic"))
        assert type(anthropic).__name__ == "AnthropicModelClient"
        assert anthropic.model_name == "claude-opus-5"

        openai = build_model_client(Settings(provider="openai"))
        assert type(openai).__name__ == "OpenAIModelClient"

    def test_a_missing_credential_does_not_raise_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both adapters must report the problem, not explode while being built.

        The OpenAI SDK raises on construction without a key; normalizing that is
        what lets the API answer 503 instead of 500.
        """
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = build_model_client(Settings(provider="openai"))
        assert credential_available(client) is False

    async def test_calling_an_unconfigured_adapter_raises_a_provider_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.llm.errors import ProviderError

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = build_model_client(Settings(provider="openai"))
        with pytest.raises(ProviderError):
            await client.create(system="s", messages=[], tools=[])

    def test_unknown_provider_is_rejected_at_configuration_time(self) -> None:
        with pytest.raises(ValueError, match="unknown provider"):
            Settings(provider="cohere")

    def test_explicit_model_overrides_the_provider_default(self) -> None:
        assert Settings(provider="openai", model="gpt-4o-mini").model == "gpt-4o-mini"

    def test_credential_check_tolerates_clients_without_one(self) -> None:
        class Bare:
            model_name = "x"

        assert credential_available(Bare()) is True
