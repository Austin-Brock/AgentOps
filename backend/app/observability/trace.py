"""Minimal run tracing.

Phase 10 replaces this with OpenTelemetry spans and a dashboard. What matters
now is that every run already produces an ordered, inspectable record of what
the agent did — including the tool calls that were refused.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    elapsed_ms: float
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        seconds, millis = divmod(int(self.elapsed_ms), 1000)
        summary = ", ".join(f"{k}={v}" for k, v in self.detail.items())
        return f"[{seconds:02d}.{millis:03d}] {self.kind}" + (f"  {summary}" if summary else "")


@dataclass
class RunTrace:
    run_id: str
    events: list[TraceEvent] = field(default_factory=list)
    _started: float = field(default_factory=time.perf_counter)

    def record(self, kind: str, **detail: Any) -> TraceEvent:
        event = TraceEvent(
            elapsed_ms=(time.perf_counter() - self._started) * 1000,
            kind=kind,
            detail=detail,
        )
        self.events.append(event)
        return event

    def render(self) -> str:
        header = f"Trace: {self.run_id}"
        return "\n".join([header, *(e.render() for e in self.events)])

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]
