"""The event wire format — step 2 of Phase 1.5.

The recorder, the replay endpoint, the live endpoint, and the frontend all read
these bytes. The format is mechanical, so these tests are mostly about the two
ways it could quietly break: an event that will not serialize, and an SSE frame
a browser silently ignores.
"""

from __future__ import annotations

import json

from app.demo.wire import encode_event, encode_events, sse_comment, sse_frame
from app.llm.scripted import call, say
from app.observability.trace import RunTrace


class TestEncoding:
    def test_an_event_carries_its_place_kind_time_and_both_payloads(self) -> None:
        trace = RunTrace(run_id="t-1")
        event = trace.record("tool_succeeded", tool="lookup_order", data={"result": {"ok": True}})

        encoded = encode_event(event, 7)
        assert encoded["seq"] == 7
        assert encoded["kind"] == "tool_succeeded"
        assert encoded["detail"] == {"tool": "lookup_order"}
        assert encoded["data"] == {"result": {"ok": True}}
        assert isinstance(encoded["elapsed_ms"], float)

    def test_elapsed_time_is_rounded(self) -> None:
        """A file replayed at human speed does not need sub-microsecond precision."""
        trace = RunTrace(run_id="t-1")
        event = trace.record("request_received")
        assert encode_event(event, 0)["elapsed_ms"] == round(event.elapsed_ms, 1)

    def test_a_whole_run_is_numbered_from_zero_in_order(self) -> None:
        trace = RunTrace(run_id="t-1")
        for kind in ("request_received", "model_responded", "run_finished"):
            trace.record(kind)

        encoded = encode_events(trace.events)
        assert [e["seq"] for e in encoded] == [0, 1, 2]
        assert [e["kind"] for e in encoded] == [
            "request_received",
            "model_responded",
            "run_finished",
        ]


class TestEveryEventARealRunProducesSurvivesTheWire:
    async def test_a_full_refund_run_round_trips(self, make_agent) -> None:
        """Encode a real run, serialize it, parse it back, and compare."""
        agent, _ = make_agent(
            [
                call("lookup_order", {"order_id": "4471"}),
                call("get_refund_policy", {}),
                call("apply_account_credit", {"customer_id": "C-1001", "amount_cents": 4299}),
                call(
                    "create_refund_request",
                    {"order_id": "4471", "reason": "damaged_on_arrival", "amount_cents": 4299},
                ),
                say("I've approved a $42.99 refund for order 4471."),
            ]
        )
        run = await agent.run("My order #4471 arrived damaged.")

        encoded = encode_events(run.trace.events)
        assert json.loads(json.dumps(encoded)) == encoded
        # The kinds a page has to be able to draw, including the refusal.
        kinds = {e["kind"] for e in encoded}
        assert {"tool_succeeded", "tool_refused", "run_finished"} <= kinds
        assert len(encoded) == len(run.trace.events)


class TestSSEFraming:
    def test_a_frame_is_an_id_and_one_data_line(self) -> None:
        frame = sse_frame({"seq": 3, "kind": "model_responded", "data": {}})
        assert frame.startswith("id: 3\ndata: {")
        # Two content lines and the blank line that terminates the frame.
        assert frame.endswith("\n\n")
        id_line, data_line, terminator = frame.split("\n", 2)
        assert id_line == "id: 3"
        assert data_line.startswith("data: ")
        assert terminator == "\n"

    def test_the_payload_is_one_line_so_the_frame_cannot_split(self) -> None:
        """A newline inside the JSON would end the frame early and truncate the event."""
        frame = sse_frame(
            {"seq": 0, "kind": "run_finished", "data": {"final_text": "Line one\nLine two"}}
        )
        body = frame.splitlines()[1].removeprefix("data: ")
        assert json.loads(body)["data"]["final_text"] == "Line one\nLine two"

    def test_the_kind_travels_inside_the_payload(self) -> None:
        """No `event:` field on purpose — a named SSE event never reaches `onmessage`."""
        frame = sse_frame({"seq": 0, "kind": "tool_refused", "data": {}})
        assert "event:" not in frame
        assert json.loads(frame.splitlines()[1].removeprefix("data: "))["kind"] == "tool_refused"

    def test_a_comment_frame_carries_no_data(self) -> None:
        assert sse_comment("keep-alive") == ": keep-alive\n\n"
