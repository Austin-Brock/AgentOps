"""Minimal run tracing, and the live event stream built on top of it.

Phase 10 replaces this with OpenTelemetry spans and a dashboard. What matters
now is that every run already produces an ordered, inspectable record of what
the agent did — including the tool calls that were refused.

That record is also what the Phase 1.5 demo needs to stream, so the trace grew a
subscriber rather than growing a twin (DD-012). Two details make one type serve
both jobs:

    detail   small, scalar, and rendered — the human-readable log line
    data     the full structures a UI needs (arguments, payloads, final text),
             carried past `render()` untouched

A consumer that wants the live view subscribes; everything else keeps reading
`events` after the run, exactly as before.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    elapsed_ms: float
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)
    # Structures too large to render but needed by a consumer watching live.
    # Always JSON-serializable: an SSE endpoint encodes this field directly.
    data: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        seconds, millis = divmod(int(self.elapsed_ms), 1000)
        summary = ", ".join(f"{k}={v}" for k, v in self.detail.items())
        return f"[{seconds:02d}.{millis:03d}] {self.kind}" + (f"  {summary}" if summary else "")


# Called once per event, as it happens. Deliberately synchronous: `record()` is
# called from synchronous code in the tool executor, so an async subscriber
# could not be awaited there without restructuring the layer that matters most.
# Sync is also sufficient for the streaming case — an SSE endpoint hands events
# to `asyncio.Queue.put_nowait`, which is sync.
EventSubscriber = Callable[[TraceEvent], None]


@dataclass
class RunTrace:
    run_id: str
    events: list[TraceEvent] = field(default_factory=list)
    on_event: EventSubscriber | None = None
    # Why the subscriber was dropped, if it was. Kept rather than raised: a
    # browser closing a connection mid-run must not take the run down with it.
    subscriber_error: str | None = field(default=None, init=False)
    _started: float = field(default_factory=time.perf_counter)

    def record(self, kind: str, *, data: dict[str, Any] | None = None, **detail: Any) -> TraceEvent:
        event = TraceEvent(
            elapsed_ms=(time.perf_counter() - self._started) * 1000,
            kind=kind,
            detail=detail,
            data=data or {},
        )
        self.events.append(event)
        self._publish(event)
        return event

    def render(self) -> str:
        header = f"Trace: {self.run_id}"
        return "\n".join([header, *(e.render() for e in self.events)])

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def _publish(self, event: TraceEvent) -> None:
        """Hand the event to the subscriber, and never let it break the run.

        A failing subscriber is detached rather than retried. A disconnected
        consumer fails on every subsequent event, and a trace full of identical
        plumbing errors would bury the run it is supposed to describe.
        """
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception as exc:  # noqa: BLE001 - a consumer's failure is not the agent's
            self.on_event = None
            self.subscriber_error = f"{type(exc).__name__}: {exc}"
