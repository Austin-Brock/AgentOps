"""The agent loop, driven by a scripted model.

Challenges 1 through 4 of Phase 1, plus the loop mechanics that have to hold
regardless of what the model decides: bounded steps, correct message threading,
and tool failures reaching the model as data.
"""

from __future__ import annotations

from app.agents.support_agent import (
    COMPLETED,
    MAX_STEPS_EXCEEDED,
    REFUSED,
    TRUNCATED,
    SupportAgent,
)
from app.llm.scripted import LoopingModelClient, call, calls, say
from app.tools.base import Fault


class TestChallenge1ToolCalling:
    """The agent receives a request and decides which tool to call."""

    async def test_single_tool_then_answer(self, make_agent) -> None:
        agent, client = make_agent(
            [
                call("lookup_order", {"order_id": "4471"}),
                say("Order 4471 was delivered on 2026-10-06."),
            ]
        )
        run = await agent.run("Where is my order #4471?")

        assert run.status == COMPLETED
        assert run.tools_executed == ["lookup_order"]
        assert run.final_text.startswith("Order 4471")
        assert client.exhausted

    async def test_tools_are_offered_with_schemas(self, make_agent) -> None:
        agent, client = make_agent([say("Hello.")])
        await agent.run("Hi")
        assert set(client.calls[0]["tool_names"]) == {
            "lookup_order",
            "get_refund_policy",
            "create_refund_request",
            "apply_account_credit",
        }

    async def test_answer_without_tools_is_a_complete_run(self, make_agent) -> None:
        agent, _ = make_agent([say("Could you give me your order number?")])
        run = await agent.run("My package arrived broken.")
        assert run.status == COMPLETED
        assert run.invocations == []


class TestChallenge2MultiStepReasoning:
    """Later decisions depend on the results of earlier ones."""

    async def test_full_refund_path(self, make_agent, store) -> None:
        agent, client = make_agent(
            [
                call("lookup_order", {"order_id": "4471"}),
                call("get_refund_policy", {}),
                call(
                    "create_refund_request",
                    {
                        "order_id": "4471",
                        "reason": "damaged_on_arrival",
                        "amount_cents": 4299,
                        "customer_note": "Mug arrived cracked.",
                    },
                ),
                say("I've approved a $42.99 refund for order 4471 (RF-0002)."),
            ]
        )
        run = await agent.run("My order #4471 arrived broken. Can you refund it?")

        assert run.status == COMPLETED
        assert run.tools_executed == [
            "lookup_order",
            "get_refund_policy",
            "create_refund_request",
        ]
        created = store.refunds_for_order("4471")
        assert len(created) == 1
        assert created[0].status == "auto_approved"
        assert run.invocations_of("create_refund_request")[0].result.payload["status"] == (
            "auto_approved"
        )
        assert client.exhausted

    async def test_each_turn_sees_the_accumulated_history(self, make_agent) -> None:
        agent, client = make_agent(
            [
                call("lookup_order", {"order_id": "4471"}),
                call("get_refund_policy", {}),
                say("Done."),
            ]
        )
        await agent.run("Refund order 4471, it arrived damaged.")

        # Turn 1: the customer message. Turn 2 and 3 add an assistant turn and a
        # tool-result turn each time.
        assert [len(c["messages"]) for c in client.calls] == [1, 3, 5]
        second_turn = client.calls[1]["messages"]
        assert second_turn[1]["role"] == "assistant"
        assert second_turn[2]["role"] == "user"
        assert second_turn[2]["content"][0]["type"] == "tool_result"

    async def test_parallel_tool_calls_return_in_one_user_turn(self, make_agent) -> None:
        agent, client = make_agent(
            [
                calls(
                    [
                        ("lookup_order", {"order_id": "4471"}, "toolu_a"),
                        ("get_refund_policy", {}, "toolu_b"),
                    ]
                ),
                say("Both checked."),
            ]
        )
        run = await agent.run("Refund 4471 please, it's damaged.")

        assert len(run.invocations) == 2
        # Splitting results across turns teaches the model to stop asking for
        # tools in parallel, so they must arrive together.
        results_turn = client.calls[1]["messages"][2]
        assert results_turn["role"] == "user"
        assert [block["tool_use_id"] for block in results_turn["content"]] == [
            "toolu_a",
            "toolu_b",
        ]


class TestChallenge3FailureRecovery:
    """A tool fails; the agent observes it and adapts."""

    async def test_transient_failure_is_retried_successfully(self, make_agent) -> None:
        agent, _ = make_agent(
            [
                call("lookup_order", {"order_id": "4471"}, call_id="first"),
                call("lookup_order", {"order_id": "4471"}, call_id="second"),
                say("Order 4471 was delivered on 2026-10-06."),
            ],
            faults=[Fault(tool="lookup_order", times=1)],
        )
        run = await agent.run("Where is order #4471?")

        attempts = run.invocations_of("lookup_order")
        assert len(attempts) == 2
        assert attempts[0].result.code == "upstream_timeout"
        assert attempts[1].is_error is False
        assert run.status == COMPLETED

    async def test_the_failure_is_visible_to_the_next_model_turn(self, make_agent) -> None:
        agent, client = make_agent(
            [
                call("lookup_order", {"order_id": "4471"}),
                say("I couldn't reach the order system; let me try again shortly."),
            ],
            faults=[Fault(tool="lookup_order", times=1)],
        )
        await agent.run("Where is order #4471?")

        result_block = client.calls[1]["messages"][2]["content"][0]
        assert result_block["is_error"] is True
        assert "upstream_timeout" in result_block["content"]

    async def test_policy_rejection_is_reported_not_retried(self, make_agent, store) -> None:
        agent, _ = make_agent(
            [
                call("lookup_order", {"order_id": "4472"}),
                call(
                    "create_refund_request",
                    {"order_id": "4472", "reason": "changed_mind", "amount_cents": 2999},
                ),
                say("That order is outside the 30-day window, so I can't refund it."),
            ]
        )
        run = await agent.run("I bought something six months ago and want a refund. Order 4472.")

        refund_attempt = run.invocations_of("create_refund_request")[0]
        assert refund_attempt.result.code == "outside_refund_window"
        assert store.refunds_for_order("4472") == []
        assert run.status == COMPLETED


class TestChallenge4Guardrails:
    """A tool the agent is not authorized to use is blocked at execution."""

    async def test_unauthorized_tool_is_refused_and_the_agent_recovers(
        self, make_agent, store
    ) -> None:
        agent, _ = make_agent(
            [
                call("lookup_order", {"order_id": "4471"}),
                call(
                    "apply_account_credit",
                    {"customer_id": "C-1001", "amount_cents": 4299},
                ),
                call(
                    "create_refund_request",
                    {
                        "order_id": "4471",
                        "reason": "damaged_on_arrival",
                        "amount_cents": 4299,
                    },
                ),
                say("I've issued a refund of $42.99 for order 4471."),
            ]
        )
        run = await agent.run("My order #4471 arrived damaged — just credit my account.")

        refused = run.refusals
        assert [inv.tool_name for inv in refused] == ["apply_account_credit"]
        assert refused[0].result.code == "permission_denied"
        assert "apply_account_credit" not in run.tools_executed
        # It recovered through the path it *is* authorized to take.
        assert len(store.refunds_for_order("4471")) == 1
        assert run.status == COMPLETED

    async def test_refusal_is_recorded_in_the_trace(self, make_agent) -> None:
        agent, _ = make_agent(
            [
                call("apply_account_credit", {"customer_id": "C-1001", "amount_cents": 100}),
                say("I can't apply credit directly."),
            ]
        )
        run = await agent.run("Just credit my account.")
        assert "tool_refused" in run.trace.kinds()


class TestLoopMechanics:
    async def test_step_ceiling_stops_a_runaway_agent(self, settings, store, registry) -> None:
        client = LoopingModelClient("lookup_order", {"order_id": "4471"})
        agent = SupportAgent(model=client, registry=registry, store=store, settings=settings)
        run = await agent.run("Where is my order?")

        assert run.status == MAX_STEPS_EXCEEDED
        assert client.call_count == settings.max_steps
        assert len(run.steps) == settings.max_steps

    async def test_refusal_stop_reason_is_its_own_outcome(self, make_agent) -> None:
        agent, _ = make_agent([say("", stop_reason="refusal")])
        run = await agent.run("Help me with something prohibited.")
        assert run.status == REFUSED

    async def test_truncated_answer_is_not_reported_as_complete(self, make_agent) -> None:
        agent, _ = make_agent([say("Here is what I foun", stop_reason="max_tokens")])
        run = await agent.run("Explain the policy in full.")
        assert run.status == TRUNCATED

    async def test_usage_accumulates_across_steps(self, make_agent) -> None:
        agent, _ = make_agent(
            [call("lookup_order", {"order_id": "4471"}), say("Delivered 2026-10-06.")]
        )
        run = await agent.run("Where is order 4471?")
        assert run.usage.input_tokens == 20
        assert run.usage.output_tokens == 10

    async def test_trace_records_the_shape_of_the_run(self, make_agent) -> None:
        agent, _ = make_agent([call("lookup_order", {"order_id": "4471"}), say("Delivered.")])
        run = await agent.run("Where is order 4471?")
        assert run.trace.kinds() == [
            "request_received",
            "model_responded",
            "tool_requested",
            "tool_succeeded",
            "model_responded",
            "run_finished",
        ]
        assert run.run_id in run.trace.render()

    async def test_history_is_threaded_ahead_of_the_new_message(self, make_agent) -> None:
        agent, client = make_agent([say("Your mug order, #4471.")])
        history = [
            {"role": "user", "content": "I ordered two mugs."},
            {"role": "assistant", "content": "Thanks — what can I help with?"},
        ]
        await agent.run("Which order was that?", history=history)

        messages = client.calls[0]["messages"]
        assert len(messages) == 3
        assert messages[0]["content"] == "I ordered two mugs."
        assert messages[-1]["content"] == "Which order was that?"
