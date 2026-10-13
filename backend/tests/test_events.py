"""The live event stream — step 1 of Phase 1.5.

`SupportAgent.run()` only returned when the run was over, so there was nothing
for a demo page to watch. The fix was a subscriber on the trace rather than a
second loop (DD-012), which puts the burden of proof here: the stream must carry
everything a viewer needs, arrive while the run is still going, and stay exactly
in step with the trace the run returns.

The last part is the one worth testing hardest. A stream that drifts from the
record is a page that shows something the agent did not do.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from app.agents.support_agent import (
    COMPLETED,
    MAX_STEPS_EXCEEDED,
    REFUSED,
    SupportAgent,
)
from app.llm.client import ModelResponse
from app.llm.scripted import LoopingModelClient, ScriptedModelClient, call, say
from app.observability.trace import RunTrace, TraceEvent

REFUND_4471 = {
    "order_id": "4471",
    "reason": "damaged_on_arrival",
    "amount_cents": 4299,
    "customer_note": "Mug arrived cracked.",
}


class Recorder:
    """A subscriber that keeps what it was handed, in the order it arrived."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def __call__(self, event: TraceEvent) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]

    def first(self, kind: str) -> TraceEvent:
        for event in self.events:
            if event.kind == kind:
                return event
        raise AssertionError(f"no {kind!r} event in {self.kinds}")

    def of_kind(self, kind: str) -> list[TraceEvent]:
        return [event for event in self.events if event.kind == kind]


class ObservingClient(ScriptedModelClient):
    """A scripted client that notes what the subscriber had seen by each turn.

    This is how the test tells streaming from batching. If events were collected
    and flushed at the end, the subscriber would have seen nothing at all by the
    time the model was called for the second time.
    """

    def __init__(self, responses: Sequence[ModelResponse], recorder: Recorder) -> None:
        super().__init__(responses)
        self._recorder = recorder
        self.seen_by_turn: list[list[str]] = []

    async def create(self, **kwargs: Any) -> ModelResponse:
        self.seen_by_turn.append(list(self._recorder.kinds))
        return await super().create(**kwargs)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


class TestEventsArriveDuringTheRun:
    async def test_the_subscriber_sees_each_turn_before_the_next_one(
        self, recorder, store, registry, settings
    ) -> None:
        client = ObservingClient(
            [call("lookup_order", {"order_id": "4471"}), say("Delivered 2026-10-06.")],
            recorder,
        )
        agent = SupportAgent(model=client, registry=registry, store=store, settings=settings)
        await agent.run("Where is my order #4471?", on_event=recorder)

        # Turn 1: only the request had happened. Turn 2: the first model
        # response and the tool call it asked for were already streamed.
        assert client.seen_by_turn == [
            ["request_received"],
            ["request_received", "model_responded", "tool_requested", "tool_succeeded"],
        ]

    async def test_the_stream_and_the_trace_are_the_same_events(self, recorder, make_agent) -> None:
        agent, _ = make_agent([call("lookup_order", {"order_id": "4471"}), say("Delivered.")])
        run = await agent.run("Where is order 4471?", on_event=recorder)

        # Identity, not equality: one list of events, published as it grows.
        assert recorder.events == run.trace.events
        assert all(a is b for a, b in zip(recorder.events, run.trace.events, strict=True))

    async def test_no_subscriber_changes_nothing(self, make_agent) -> None:
        agent, _ = make_agent([call("lookup_order", {"order_id": "4471"}), say("Delivered.")])
        run = await agent.run("Where is order 4471?")

        assert run.status == COMPLETED
        assert run.trace.kinds() == [
            "request_received",
            "model_responded",
            "tool_requested",
            "tool_succeeded",
            "model_responded",
            "run_finished",
        ]
        assert run.trace.subscriber_error is None


class TestEventsCarryWhatTheDemoDraws:
    """The page renders personas, tool calls, arguments, results, and totals."""

    async def test_the_opening_event_carries_the_request_and_the_tools_offered(
        self, recorder, make_agent
    ) -> None:
        agent, _ = make_agent([say("Could you give me your order number?")])
        await agent.run("My package arrived broken.", customer_id="C-1001", on_event=recorder)

        data = recorder.first("request_received").data
        assert data["goal"] == "My package arrived broken."
        assert data["customer_id"] == "C-1001"
        assert "create_refund_request" in data["tools_offered"]

    async def test_a_model_turn_carries_the_calls_it_decided_on(self, recorder, make_agent) -> None:
        agent, _ = make_agent([call("lookup_order", {"order_id": "4471"}), say("Delivered.")])
        await agent.run("Where is order 4471?", on_event=recorder)

        data = recorder.of_kind("model_responded")[0].data
        assert data["step"] == 0
        assert data["tool_calls"] == [
            {
                "tool_use_id": "toolu_scripted_lookup_order_0",
                "tool": "lookup_order",
                "arguments": {"order_id": "4471"},
            }
        ]
        assert data["usage"]["output_tokens"] == 5

    async def test_a_tool_event_carries_its_arguments_and_its_result(
        self, recorder, make_agent
    ) -> None:
        agent, _ = make_agent(
            [
                call("create_refund_request", REFUND_4471),
                say("I've approved a $42.99 refund for order 4471."),
            ]
        )
        await agent.run("Refund my damaged mug, order #4471.", on_event=recorder)

        data = recorder.first("tool_succeeded").data
        assert data["tool"] == "create_refund_request"
        assert data["arguments"]["amount_cents"] == 4299
        assert data["result"]["status"] == "auto_approved"
        assert data["executed"] is True
        assert data["is_error"] is False

    async def test_the_closing_event_carries_the_answer_and_the_totals(
        self, recorder, make_agent
    ) -> None:
        agent, _ = make_agent(
            [call("lookup_order", {"order_id": "4471"}), say("Delivered 2026-10-06.")]
        )
        await agent.run("Where is order 4471?", on_event=recorder)

        data = recorder.first("run_finished").data
        assert data["status"] == COMPLETED
        assert data["final_text"] == "Delivered 2026-10-06."
        assert data["tools_executed"] == ["lookup_order"]
        assert data["tools_refused"] == []
        assert data["usage"] == {
            "input_tokens": 20,
            "output_tokens": 10,
            "reasoning_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    async def test_a_refused_call_streams_with_its_reason(self, recorder, make_agent) -> None:
        """The guardrail scenario is the point of the demo, so it has to stream."""
        agent, _ = make_agent(
            [
                call("apply_account_credit", {"customer_id": "C-1001", "amount_cents": 4299}),
                say("I can't apply credit directly — here is a refund instead."),
            ]
        )
        await agent.run("Just credit my account.", on_event=recorder)

        data = recorder.first("tool_refused").data
        assert data["tool"] == "apply_account_credit"
        assert data["executed"] is False
        assert data["error_code"] == "permission_denied"
        assert "finance:write" in data["result"]["missing_scopes"]
        # And the attempt itself is visible, not only its denial.
        assert recorder.of_kind("model_responded")[0].data["tool_calls"][0]["arguments"] == {
            "customer_id": "C-1001",
            "amount_cents": 4299,
        }

    async def test_every_event_is_json_serializable(self, recorder, make_agent) -> None:
        """An SSE endpoint encodes `data` directly, so nothing in it may be exotic."""
        agent, _ = make_agent(
            [
                call("lookup_order", {"order_id": "4471"}),
                call("get_refund_policy", {}),
                call("create_refund_request", REFUND_4471),
                say("Refund approved."),
            ]
        )
        await agent.run("Refund order #4471, it arrived cracked.", on_event=recorder)

        for event in recorder.events:
            json.dumps({"kind": event.kind, "detail": event.detail, "data": event.data})


class TestEveryRunStreamsAnEnding:
    """A viewer must never be left watching a run that silently stopped."""

    async def test_a_completed_run_ends_with_run_finished(self, recorder, make_agent) -> None:
        agent, _ = make_agent([say("Could you give me your order number?")])
        run = await agent.run("My package arrived broken.", on_event=recorder)
        assert run.status == COMPLETED
        assert recorder.kinds[-1] == "run_finished"

    async def test_a_provider_refusal_ends_with_run_finished(self, recorder, make_agent) -> None:
        agent, _ = make_agent([say("", stop_reason="refusal")])
        run = await agent.run("Help me with something prohibited.", on_event=recorder)
        assert run.status == REFUSED
        assert recorder.kinds[-1] == "run_finished"
        assert recorder.first("run_finished").data["status"] == REFUSED

    async def test_the_step_ceiling_ends_with_run_finished(
        self, recorder, store, registry, settings
    ) -> None:
        client = LoopingModelClient("lookup_order", {"order_id": "4471"})
        agent = SupportAgent(model=client, registry=registry, store=store, settings=settings)
        run = await agent.run("Where is my order?", on_event=recorder)

        assert run.status == MAX_STEPS_EXCEEDED
        assert recorder.kinds[-1] == "run_finished"
        assert recorder.first("run_finished").data["steps"] == settings.max_steps


class TestAFailingSubscriberCannotFailTheRun:
    async def test_the_run_completes_and_the_subscriber_is_detached(self, make_agent) -> None:
        """A browser closing its connection mid-run is not the agent's problem."""
        calls: list[str] = []

        def disconnected(event: TraceEvent) -> None:
            calls.append(event.kind)
            raise ConnectionResetError("client went away")

        agent, _ = make_agent([call("create_refund_request", REFUND_4471), say("Refund approved.")])
        run = await agent.run("Refund order #4471.", on_event=disconnected)

        assert run.status == COMPLETED
        assert run.tools_executed == ["create_refund_request"]
        # Dropped after the first failure rather than retried on every event.
        assert calls == ["request_received"]
        assert run.trace.subscriber_error == "ConnectionResetError: client went away"
        # The run's own record is unaffected by the consumer that abandoned it.
        assert run.trace.kinds()[-1] == "run_finished"


class TestTheTraceStaysReadable:
    def test_render_ignores_the_structured_payload(self) -> None:
        """`data` exists for consumers; the log line stays a log line."""
        trace = RunTrace(run_id="t-1")
        trace.record("tool_succeeded", tool="lookup_order", data={"result": {"status": "x" * 500}})

        line = trace.render().splitlines()[-1]
        assert "tool_succeeded" in line
        assert "tool=lookup_order" in line
        assert "x" * 500 not in line
