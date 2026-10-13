"""Recorded runs and the replay stream — step 3 and 4 of Phase 1.5.

A replay works on a machine with no credentials, which is the point of it
(DD-011) and also why it needs guarding: a recording cannot fail loudly. It
keeps serving perfectly while the agent it describes moves on without it.

So the tests here are mostly about refusing bad recordings rather than accepting
good ones: a file in the wrong format, a run that never finished, a recording
filed under the wrong scenario, and — the one that matters across the next eight
phases — a listed scenario with no recording at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.demo.catalog import MANIFEST_PATH, load_catalog
from app.demo.replay import (
    MAX_GAP_MS,
    MAX_SPEED,
    REPLAY_DIR,
    ReplayFileError,
    ReplayIndex,
    ReplayNotFound,
    build_replay,
    pacing,
    parse_replay,
    write_replay,
)
from app.evaluation.scenario import load_dataset
from app.llm.scripted import call, say
from app.main import app


@pytest.fixture
def index(tmp_path) -> ReplayIndex:
    return ReplayIndex(directory=tmp_path / "replays")


@pytest.fixture
async def recorded(make_agent, settings: Settings):
    """A recording built from a real (scripted) run, not a hand-written fixture."""
    agent, _ = make_agent(
        [
            call("lookup_order", {"order_id": "4471"}),
            call("get_refund_policy", {}),
            call("apply_account_credit", {"customer_id": "C-1001", "amount_cents": 4299}),
            call(
                "create_refund_request",
                {"order_id": "4471", "reason": "damaged_on_arrival", "amount_cents": 4299},
            ),
            say("I've approved a $42.99 refund for order 4471 (RF-0002)."),
        ]
    )
    run = await agent.run("My order #4471 arrived damaged. I'd like a refund.")
    return build_replay(
        scenario_id="S01",
        run=run,
        settings=settings,
        recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        git_sha="abc123def456789",
        git_dirty=False,
    )


class TestTheRecordedFormat:
    def test_it_records_what_was_running_when_it_was_captured(self, recorded) -> None:
        """Without this a stale replay is indistinguishable from a fresh one."""
        assert recorded["git_sha"] == "abc123def456789"
        assert recorded["git_dirty"] is False
        assert recorded["model"] == "scripted"
        assert recorded["provider"] == "anthropic"
        assert recorded["recorded_at"].endswith("+00:00")

    def test_it_records_the_whole_run_not_a_summary(self, recorded) -> None:
        kinds = [event["kind"] for event in recorded["events"]]
        assert kinds[0] == "request_received"
        assert kinds[-1] == "run_finished"
        assert "tool_refused" in kinds
        assert recorded["tools_executed"] == [
            "lookup_order",
            "get_refund_policy",
            "create_refund_request",
        ]
        assert recorded["tools_refused"] == ["apply_account_credit"]
        assert recorded["status"] == "completed"

    def test_it_survives_a_trip_through_disk(self, recorded, index) -> None:
        path = write_replay(recorded, index=index)
        assert json.loads(path.read_text()) == recorded

        replay = index.load("S01")
        assert replay.events == tuple(recorded["events"])
        assert replay.final_text.startswith("I've approved")
        assert replay.duration_ms == recorded["events"][-1]["elapsed_ms"]


class TestABadRecordingIsRefused:
    def test_a_format_from_another_version_is_refused(self, recorded) -> None:
        with pytest.raises(ReplayFileError, match="wire version"):
            parse_replay({**recorded, "wire_version": 99})

    def test_a_recording_with_no_events_is_refused(self, recorded) -> None:
        with pytest.raises(ReplayFileError, match="nothing to replay"):
            parse_replay({**recorded, "events": []})

    def test_a_run_that_never_finished_is_refused(self, recorded) -> None:
        """A stream with no ending leaves the page waiting forever."""
        with pytest.raises(ReplayFileError, match="not 'run_finished'"):
            parse_replay({**recorded, "events": recorded["events"][:-1]})

    def test_a_recording_missing_its_metadata_is_refused(self, recorded) -> None:
        with pytest.raises(ReplayFileError, match="missing field 'git_sha'"):
            parse_replay({k: v for k, v in recorded.items() if k != "git_sha"})

    def test_a_recording_filed_under_the_wrong_scenario_is_refused(self, recorded, index) -> None:
        """Serving one customer's run captioned as another's is worse than a 404."""
        write_replay({**recorded, "scenario_id": "S05"}, index=index)
        (index.path_for("S05")).rename(index.path_for("S01"))
        with pytest.raises(ReplayFileError, match="records scenario 'S05'"):
            index.load("S01")

    def test_a_missing_recording_is_its_own_outcome(self, index) -> None:
        with pytest.raises(ReplayNotFound):
            index.load("S01")
        assert index.meta("S01")["available"] is False


class TestStaleness:
    """The checks that have to keep holding for the next eight phases."""

    def test_every_listed_scenario_has_a_recording(self) -> None:
        catalog = load_catalog(MANIFEST_PATH, dataset=load_dataset())
        missing = sorted(set(catalog.ids) - ReplayIndex().ids())
        assert not missing, (
            f"no recording for {missing}. Run `make record-replays` — a listed scenario "
            "with no replay is a dead button on the page"
        )

    def test_no_recording_refers_to_a_scenario_that_is_no_longer_listed(self) -> None:
        catalog = load_catalog(MANIFEST_PATH, dataset=load_dataset())
        orphans = sorted(ReplayIndex().ids() - set(catalog.ids))
        assert not orphans, (
            f"recordings exist for {orphans}, which the manifest no longer lists. "
            "Delete them rather than leaving runs on disk that nothing serves"
        )

    def test_every_shipped_recording_loads_and_is_internally_consistent(self) -> None:
        index = ReplayIndex()
        for scenario_id in sorted(index.ids()):
            replay = index.load(scenario_id)
            assert replay.events[-1]["kind"] == "run_finished"
            assert replay.git_sha
            assert replay.model
            # Numbered from zero with no gaps, so the client can order on `seq`.
            assert [e["seq"] for e in replay.events] == list(range(len(replay.events)))

    def test_at_least_one_recording_shows_a_tool_call_refused_in_code(self) -> None:
        """The page's central claim needs a real run that demonstrates it.

        The first eight recordings contained no refusal at all: on every one of
        them the agent read the policy and stayed inside its authority, which is
        good behavior and a terrible demonstration. S21 was added to the dataset
        for exactly this, and this test exists so a future re-record cannot
        quietly lose it.
        """
        index = ReplayIndex()
        refusals = {
            scenario_id: [
                event["data"]["error_code"]
                for event in index.load(scenario_id).events
                if event["kind"] == "tool_refused"
            ]
            for scenario_id in sorted(index.ids())
        }
        assert any(codes for codes in refusals.values()), (
            "no shipped recording contains a refused tool call, so the page cannot show "
            f"a guardrail firing in code. Re-record S21. Refusals found: {refusals}"
        )
        assert "permission_denied" in [code for codes in refusals.values() for code in codes]

    def test_every_recording_replays_in_a_watchable_time(self) -> None:
        """Capped pacing has to hold for the slow runs, not only the fast ones."""
        index = ReplayIndex()
        for scenario_id in sorted(index.ids()):
            replay = index.load(scenario_id)
            total_s = sum(pacing(replay.events))
            assert 1 < total_s < 12, (
                f"{scenario_id} replays in {total_s:.1f}s (the real run took "
                f"{replay.duration_ms / 1000:.1f}s)"
            )

    def test_the_recordings_live_where_the_index_looks(self) -> None:
        assert REPLAY_DIR.name == "replays"
        assert REPLAY_DIR.parent.name == "demo"


class TestPacing:
    """Replayed at the pace the run had, minus the waits nobody should sit through."""

    def test_each_wait_is_the_gap_the_run_actually_had(self) -> None:
        events = [{"elapsed_ms": 0.0}, {"elapsed_ms": 250.0}, {"elapsed_ms": 1_000.0}]
        assert pacing(events) == [0.0, 0.25, 0.75]

    def test_a_long_think_is_capped(self) -> None:
        events = [{"elapsed_ms": 0.0}, {"elapsed_ms": 10_600.0}]
        assert pacing(events) == [0.0, MAX_GAP_MS / 1000]

    def test_speed_divides_every_wait(self) -> None:
        events = [{"elapsed_ms": 0.0}, {"elapsed_ms": 1_000.0}]
        assert pacing(events, speed=4) == [0.0, 0.25]

    def test_speed_is_clamped_so_a_request_cannot_stall_a_worker(self) -> None:
        events = [{"elapsed_ms": 0.0}, {"elapsed_ms": 1_000.0}]
        assert pacing(events, speed=0.0) == pacing(events, speed=0.1)
        assert pacing(events, speed=-5) == pacing(events, speed=0.1)
        assert pacing(events, speed=10**9) == pacing(events, speed=MAX_SPEED)

    def test_a_non_monotonic_recording_never_waits_backwards(self) -> None:
        events = [{"elapsed_ms": 500.0}, {"elapsed_ms": 100.0}]
        assert pacing(events) == [0.5, 0.0]

    def test_the_shipped_recording_replays_in_a_watchable_time(self) -> None:
        """The cap, checked against the real run rather than a made-up one."""
        replay = ReplayIndex().load("S01")
        total_s = sum(pacing(replay.events))
        assert replay.duration_ms / 1000 > 15, "the real run was slower than this asserts"
        assert 3 < total_s < 8, f"replays in {total_s:.1f}s; too slow to watch or too fast to read"


class TestTheReplayStream:
    @pytest.fixture
    def client(self) -> TestClient:
        return TestClient(app)

    def test_it_streams_the_recorded_events_in_order(self, client: TestClient) -> None:
        with client.stream("GET", "/demo/scenarios/S01/replay?speed=1000") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join(response.iter_text())

        events = [
            json.loads(line.removeprefix("data: "))
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        assert [e["seq"] for e in events] == list(range(len(events)))
        assert events[0]["kind"] == "request_received"
        assert events[-1]["kind"] == "run_finished"

    def test_it_opens_with_a_comment_so_a_buffering_proxy_shows_itself(
        self, client: TestClient
    ) -> None:
        with client.stream("GET", "/demo/scenarios/S01/replay?speed=1000") as response:
            first = next(iter(response.iter_lines()))
        assert first.startswith(": replay S01 recorded ")

    def test_the_stream_carries_the_payloads_the_page_draws(self, client: TestClient) -> None:
        with client.stream("GET", "/demo/scenarios/S01/replay?speed=1000") as response:
            body = "".join(response.iter_text())
        events = [
            json.loads(line.removeprefix("data: "))
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        finished = next(e for e in events if e["kind"] == "run_finished")
        assert finished["data"]["final_text"]
        assert finished["data"]["usage"]["input_tokens"] > 0

        # The outcome events are the ones a page draws a result from.
        # `tool_requested` is a compact trace line with no payload: the model's
        # requested arguments already arrived on the preceding model_responded.
        outcomes = [
            e for e in events if e["kind"] in {"tool_succeeded", "tool_failed", "tool_refused"}
        ]
        assert outcomes, "a recorded run with no tool calls is not worth showing"
        for event in outcomes:
            assert "arguments" in event["data"]
            assert "result" in event["data"]

        decided = [e for e in events if e["kind"] == "model_responded"]
        assert any(call["arguments"] for e in decided for call in e["data"]["tool_calls"])

    def test_an_unlisted_scenario_is_a_404(self, client: TestClient) -> None:
        """S02 is in the dataset but not on the demo's list."""
        response = client.get("/demo/scenarios/S02/replay")
        assert response.status_code == 404
        assert "S02" in response.json()["detail"]

    @pytest.mark.parametrize("crafted", ["S99", "../../etc/passwd", "S01%20approve"])
    def test_a_crafted_id_is_a_404_not_a_file_read(self, client: TestClient, crafted) -> None:
        assert client.get(f"/demo/scenarios/{crafted}/replay").status_code == 404
