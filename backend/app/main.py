"""HTTP surface.

Thin on purpose: the API constructs an agent and returns its run. All the
behavior worth testing lives below this layer, which is why the test suite
drives the agent directly rather than going through HTTP.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agents.support_agent import AgentRun, SupportAgent
from app.config import get_settings
from app.data.store import DataStore
from app.demo.catalog import DemoScenario, UnknownDemoScenario, get_catalog
from app.demo.live import LiveGate, build_gate
from app.demo.replay import (
    TERMINAL_KIND,
    Replay,
    ReplayFileError,
    ReplayIndex,
    ReplayNotFound,
    pacing,
)
from app.demo.wire import encode_event, sse_comment, sse_frame, sse_notice
from app.llm.errors import CREDENTIAL_ENV_VARS, ProviderError
from app.llm.factory import build_model_client, credential_available
from app.observability.trace import TraceEvent
from app.tools.base import ToolContext
from app.tools.toolset import SUPPORT_AGENT_SCOPES, build_support_toolset

app = FastAPI(title="AgentOps", version="0.1.0")

_store = DataStore()
_registry = build_support_toolset()
_replays = ReplayIndex()

#: The live-mode controls, built once per process because the rate limit and the
#: daily budget are counters and a per-request gate would count nothing. That is
#: also the limitation: these counters are per instance, so a platform running
#: several multiplies the daily ceiling by the instance count (DD-015).
_gate: LiveGate = build_gate(
    get_settings(),
    has_credential=lambda: credential_available(build_model_client(get_settings())),
)


def _visitor(request: Request) -> str:
    """Who to rate-limit, as well as a single process can know.

    Vercel terminates TLS and proxies, so the socket peer is the platform and
    `x-forwarded-for` holds the chain. The left-most entry is the client as the
    first proxy saw it — spoofable by a determined caller, which is why the rate
    limit is not the only control and why the budget behind it is keyed on
    nothing at all.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if first := forwarded.split(",")[0].strip():
        return first
    return request.client.host if request.client else "unknown"


class RunRequest(BaseModel):
    message: str = Field(description="The customer's message.")
    customer_id: str | None = Field(default=None, description="Authenticated customer, if known.")


class ToolCallView(BaseModel):
    tool: str
    arguments: dict[str, Any]
    executed: bool
    is_error: bool
    error_code: str | None


class RunResponse(BaseModel):
    run_id: str
    status: str
    response: str
    tool_calls: list[ToolCallView]
    trace: list[str]
    usage: dict[str, int]


def _view(run: AgentRun) -> RunResponse:
    return RunResponse(
        run_id=run.run_id,
        status=run.status,
        response=run.final_text,
        tool_calls=[
            ToolCallView(
                tool=inv.tool_name,
                arguments=inv.arguments,
                executed=inv.executed,
                is_error=inv.is_error,
                error_code=inv.result.code,
            )
            for inv in run.invocations
        ],
        trace=[event.render() for event in run.trace.events],
        usage={
            "input_tokens": run.usage.input_tokens,
            "output_tokens": run.usage.output_tokens,
        },
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "provider": settings.provider,
        "model": settings.model,
        "orders_loaded": len(_store.orders),
        "tools": len(_registry),
    }


@app.get("/tools")
async def list_tools() -> dict[str, Any]:
    settings = get_settings()
    ctx = ToolContext(store=_store, settings=settings, granted_scopes=SUPPORT_AGENT_SCOPES)
    return {
        "granted_scopes": sorted(SUPPORT_AGENT_SCOPES),
        "tools": [
            {
                "name": tool.name,
                "required_scopes": sorted(tool.required_scopes),
                "authorized": tool.required_scopes <= SUPPORT_AGENT_SCOPES,
                "destructive": tool.destructive,
            }
            for tool in _registry
        ],
        "advertised_to_model": [spec["name"] for spec in _registry.specs(ctx)],
    }


@app.get("/demo/scenarios")
async def demo_scenarios(request: Request) -> dict[str, Any]:
    """What a visitor may click, with the order facts looked up fresh.

    No message is accepted here or anywhere else in the demo: the client sends a
    scenario id and the server resolves the prompt from the evaluation dataset
    (DD-011). The list is small, fixed, and the only thing a run can be about.

    Carries the live-mode status too, so the page can disable the button and say
    why rather than offering one that fails. Reported per visitor, which is what
    makes "you have used your runs for this hour" something the page can state
    before a click instead of after one.
    """
    return {
        **get_catalog().view(_store, get_settings(), _replays),
        "live": _gate.status(_visitor(request)),
    }


@app.get("/demo/scenarios/{scenario_id}/replay")
async def demo_replay(scenario_id: str, speed: float = 1.0) -> StreamingResponse:
    """Stream a recorded run as it originally happened.

    Paced from the recorded `elapsed_ms` rather than dumped at once, because
    watching the agent decide is the point. Gaps are capped: a model that
    thought for nine seconds reads as a hung page, not as deliberation.
    """
    try:
        get_catalog().resolve(scenario_id)
        replay = _replays.load(scenario_id)
    except UnknownDemoScenario:
        raise HTTPException(status_code=404, detail=f"No demo scenario {scenario_id!r}.") from None
    except ReplayNotFound:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Scenario {scenario_id!r} has no recorded run yet. "
                "Run `make record-replays` to capture one."
            ),
        ) from None
    except ReplayFileError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StreamingResponse(
        _replay_events(replay, speed=speed),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Nginx and DO's router buffer responses by default, which turns a
            # progressive stream into one block delivered at the end.
            "X-Accel-Buffering": "no",
        },
    )


async def _replay_events(replay: Replay, *, speed: float) -> AsyncIterator[str]:
    # Flushed before the first event so a buffering proxy reveals itself
    # immediately rather than at the end of the run.
    yield sse_comment(f"replay {replay.scenario_id} recorded {replay.recorded_at}")

    for wait_s, event in zip(pacing(replay.events, speed=speed), replay.events, strict=True):
        if wait_s > 0:
            await asyncio.sleep(wait_s)
        yield sse_frame(event)


@app.post("/demo/scenarios/{scenario_id}/run")
async def demo_run(scenario_id: str, request: Request) -> StreamingResponse:
    """Run a demo scenario against the real model, if every control allows it.

    **POST rather than GET**, even though `EventSource` can only issue a GET and
    the client therefore has to read the stream by hand. A GET that spends money
    is a GET that a link prefetcher, a crawler, or a chat client's unfurler will
    eventually fire on its own, and the first the budget would know about it is
    an empty balance.

    **Always a stream, never an error.** Anything short of an unknown scenario
    id answers 200 with a `notice` frame naming the control that fired, followed
    by the recorded run of the same scenario. A visitor who clicks Run live on a
    spent budget is owed an explanation and the agent working, not a stack trace
    (DD-015).

    The client still sends only a scenario id. The prompt is resolved from the
    evaluation dataset exactly as replay resolves it, so opening a paid endpoint
    does not open an input (DD-011).
    """
    try:
        scenario = get_catalog().resolve(scenario_id)
    except UnknownDemoScenario:
        raise HTTPException(status_code=404, detail=f"No demo scenario {scenario_id!r}.") from None

    decision = _gate.admit(_visitor(request))
    if not decision.is_live:
        # Degrading needs a recording to degrade *to*. Without one there is
        # nothing to show, and that is the one case worth an error — it means a
        # scenario shipped without `make record-replays`, not that a visitor did
        # anything wrong.
        try:
            replay = _replays.load(scenario_id)
        except (ReplayNotFound, ReplayFileError) as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Live mode is unavailable ({decision.reason}) and scenario "
                    f"{scenario_id!r} has no recording to fall back to: {exc}"
                ),
            ) from None
        return _stream(_degraded_events(replay, decision.notice()))

    return _stream(_live_events(scenario))


def _stream(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _degraded_events(replay: Replay, notice: dict[str, Any] | None) -> AsyncIterator[str]:
    """A live request that was refused: say so first, then replay.

    The notice goes out before the first event so the page can label the trace
    correctly from the start rather than retitling it afterwards.
    """
    if notice:
        yield sse_notice(notice)
    async for frame in _replay_events(replay, speed=1.0):
        yield frame


async def _live_events(scenario: DemoScenario) -> AsyncIterator[str]:
    """Stream a real run as it happens, and always send an ending.

    The subscriber is a `put_nowait` onto a queue, which is why `on_event` is
    synchronous (DD-012): the tool executor records from synchronous code, so
    there is nothing to await it from.

    The ending is the part that needs care. Every exit from the agent loop
    records `run_finished`, so a completed run ends itself — but a `ProviderError`
    raised inside `model.create()` leaves the loop without reaching `_finish`,
    and the client closes its stream on the terminal event and nothing else. A
    failed run therefore gets a synthetic ending here, or the page watches a
    stream that will never finish.
    """
    settings = get_settings()
    queue: asyncio.Queue[TraceEvent] = asyncio.Queue()
    agent = SupportAgent(
        model=build_model_client(settings),
        registry=_registry,
        store=_store,
        settings=settings,
    )

    yield sse_notice({"mode": "live", "reason": None, "message": None})

    run_task = asyncio.create_task(agent.run(scenario.message, on_event=queue.put_nowait))
    seq = 0
    ended = False
    charged = False
    try:
        while not ended:
            pending = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({pending, run_task}, return_when=asyncio.FIRST_COMPLETED)

            if pending in done:
                event = pending.result()
                yield sse_frame(encode_event(event, seq))
                seq += 1
                ended = event.kind == TERMINAL_KIND
                continue

            # The run task settled without the stream reaching a terminal event.
            # Drain what was recorded first: a run that finished normally in the
            # same tick leaves its ending in the queue, and reporting a failure
            # on top of a successful run would be a lie the page then displays.
            pending.cancel()
            while not queue.empty():
                event = queue.get_nowait()
                yield sse_frame(encode_event(event, seq))
                seq += 1
                ended = ended or event.kind == TERMINAL_KIND

            if not ended:
                # Nothing recorded an ending, so the loop raised before reaching
                # `_finish`. Synthesize one or the page waits forever.
                yield sse_notice(_failure_notice(run_task))
                yield sse_frame(_synthetic_ending(seq))
            break

        if ended:
            # Safe to await: an ending was recorded, so the loop returned rather
            # than raised. The failure path never gets here, which is the point
            # — awaiting a task that raised would re-raise it into the response
            # body, after the headers have already gone out.
            run = await run_task
            _gate.finish(run.usage.input_tokens + run.usage.output_tokens)
            charged = True
    finally:
        if not charged:
            # Either the run failed before reporting usage, or the client
            # disconnected and this generator was closed mid-stream. Both leave
            # a reservation and a concurrency slot held, and releasing them is
            # not optional: a leaked reservation shrinks the day's capacity for
            # everyone with nothing to show for it.
            #
            # The honest gap: a disconnected run may already have spent tokens,
            # and they go uncounted because usage is only known from a run that
            # returns. So a visitor who disconnects repeatedly spends without
            # being charged — bounded by the rate limit to their few runs an
            # hour, which is why that control is not redundant with the budget.
            run_task.cancel()
            _gate.abandon()


def _failure_notice(run_task: asyncio.Task[AgentRun]) -> dict[str, Any]:
    """What the page says when a live run dies mid-stream."""
    exc = run_task.exception() if run_task.done() and not run_task.cancelled() else None
    detail = f"{type(exc).__name__}: {exc}" if exc else "the run ended unexpectedly"
    return {
        "mode": "live",
        "reason": "run_failed",
        "message": (
            "The live run stopped before it finished — the model provider "
            f"failed ({detail}). Press Replay to watch a recorded run instead."
        ),
    }


def _synthetic_ending(seq: int) -> dict[str, Any]:
    """A terminal frame for a run that never recorded one.

    Shaped like the real `run_finished` so the page needs no special case, and
    labelled `provider_error` so it is not mistaken for a run that completed.
    """
    return {
        "seq": seq,
        "kind": TERMINAL_KIND,
        "elapsed_ms": 0.0,
        "detail": {"status": "provider_error", "steps": 0},
        "data": {
            "run_id": "",
            "status": "provider_error",
            "final_text": "",
            "steps": 0,
            "tools_executed": [],
            "tools_refused": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }


@app.post("/runs", response_model=RunResponse)
async def create_run(request: RunRequest) -> RunResponse:
    """Run the agent against arbitrary text. Closed unless explicitly opened.

    This is the one endpoint in the service that accepts a free-form prompt, so
    on a public origin it is precisely the open input the fixed-scenario demo
    exists to avoid (DD-011) — and it was reachable in production, answering 503
    only because no credential happened to be deployed. A key arriving for live
    mode would have turned that accident into an open tap.

    Closed with a 404 rather than a 403: a 403 confirms the endpoint is there
    and invites someone to look for the way in. There is nothing to find here.
    """
    settings = get_settings()
    if not settings.open_runs_enabled:
        raise HTTPException(status_code=404, detail="Not Found")

    model = build_model_client(settings)
    if not credential_available(model):
        raise HTTPException(
            status_code=503,
            detail=(
                f"No {settings.provider} credential resolved. Set "
                f"{CREDENTIAL_ENV_VARS[settings.provider]} and retry."
            ),
        )

    agent = SupportAgent(model=model, registry=_registry, store=_store, settings=settings)
    try:
        run = await agent.run(request.message, customer_id=request.customer_id)
    except ProviderError as exc:
        # The provider failed, not the agent. Phase 8 turns this into routing and
        # fallback; for now it is reported honestly as an upstream failure.
        raise HTTPException(status_code=502, detail=f"Model provider error: {exc}") from exc
    return _view(run)
