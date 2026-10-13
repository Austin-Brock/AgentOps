"""Recorded runs: the format, and reading them back.

A replay is one real run against a real model, written down event for event. It
is the demo's default mode rather than a fallback (DD-011): it costs nothing,
is identical every time, and works on a machine with no credentials at all, so
the page cannot go blank because a key expired or a rate limit was hit.

What makes a recording honest is the metadata. Every file records the **git SHA,
model id, and timestamp** it was captured under, because the real risk here is
not that a replay breaks — it is that a replay keeps working perfectly while
quietly demonstrating last month's agent. The capture date goes on screen, and
`make record-replays` is part of closing each phase rather than a thing someone
remembers to do.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents.support_agent import AgentRun
from app.config import REPO_ROOT, Settings
from app.demo.wire import WIRE_VERSION, encode_events

REPLAY_DIR = REPO_ROOT / "data" / "demo" / "replays"

TERMINAL_KIND = "run_finished"

#: A recorded pause longer than this is replayed as this. The original timing
#: stays in the file; what is capped is only how long a viewer waits. Chosen
#: against the real S01 recording, whose two longest pauses were 7.6s (deciding
#: to create the refund) and 10.6s (writing the final reply). Uncapped, a
#: visitor spends 22 seconds on one scenario, 18 of them watching nothing.
MAX_GAP_MS = 1_500.0
MIN_SPEED = 0.1
MAX_SPEED = 1_000.0


class ReplayFileError(Exception):
    """A recording on disk is not usable. Loud, because the page depends on it."""


class ReplayNotFound(KeyError):
    """No recording exists for this scenario yet."""


@dataclass(frozen=True)
class Replay:
    scenario_id: str
    recorded_at: str
    git_sha: str
    git_dirty: bool
    provider: str
    model: str
    effort: str
    status: str
    final_text: str
    usage: dict[str, int]
    events: tuple[dict[str, Any], ...]

    @property
    def duration_ms(self) -> float:
        """How long the recorded run actually took, for pacing the replay."""
        return self.events[-1]["elapsed_ms"] if self.events else 0.0

    def meta(self) -> dict[str, Any]:
        """What the page shows about the recording itself.

        On screen so that a stale replay is visible rather than invisible: a
        visitor can see which model answered, and when.
        """
        return {
            "available": True,
            "recorded_at": self.recorded_at,
            "git_sha": self.git_sha[:12],
            "git_dirty": self.git_dirty,
            "provider": self.provider,
            "model": self.model,
            "events": len(self.events),
            "duration_ms": self.duration_ms,
        }


def build_replay(
    *,
    scenario_id: str,
    run: AgentRun,
    settings: Settings,
    recorded_at: str,
    git_sha: str,
    git_dirty: bool,
) -> dict[str, Any]:
    """Serialize a completed run into the on-disk form."""
    return {
        "wire_version": WIRE_VERSION,
        "scenario_id": scenario_id,
        "recorded_at": recorded_at,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "provider": settings.provider,
        "model": settings.model,
        "effort": settings.effort,
        "run_id": run.run_id,
        "status": run.status,
        "final_text": run.final_text,
        "usage": {
            "input_tokens": run.usage.input_tokens,
            "output_tokens": run.usage.output_tokens,
        },
        "tools_executed": run.tools_executed,
        "tools_refused": [inv.tool_name for inv in run.refusals],
        "events": encode_events(run.trace.events),
    }


def parse_replay(payload: dict[str, Any], *, source: str = "payload") -> Replay:
    """Read a recording, refusing anything the page could not draw correctly."""
    version = payload.get("wire_version")
    if version != WIRE_VERSION:
        raise ReplayFileError(
            f"{source}: wire version {version!r}, expected {WIRE_VERSION}. Re-record it "
            "rather than serving a format the page no longer speaks"
        )

    events = payload.get("events") or []
    if not events:
        raise ReplayFileError(f"{source}: no events, so there is nothing to replay")
    if events[-1].get("kind") != TERMINAL_KIND:
        raise ReplayFileError(
            f"{source}: last event is {events[-1].get('kind')!r}, not {TERMINAL_KIND!r}. "
            "A recording that does not end leaves a viewer watching forever"
        )

    try:
        return Replay(
            scenario_id=payload["scenario_id"],
            recorded_at=payload["recorded_at"],
            git_sha=payload["git_sha"],
            git_dirty=bool(payload.get("git_dirty", False)),
            provider=payload["provider"],
            model=payload["model"],
            effort=payload.get("effort", ""),
            status=payload["status"],
            final_text=payload.get("final_text", ""),
            usage=payload.get("usage", {}),
            events=tuple(events),
        )
    except KeyError as exc:
        raise ReplayFileError(f"{source}: missing field {exc.args[0]!r}") from None


def pacing(
    events: Sequence[dict[str, Any]],
    *,
    speed: float = 1.0,
    max_gap_ms: float = MAX_GAP_MS,
) -> list[float]:
    """Seconds to wait before each event, from the gaps the run actually had.

    Replayed at the original pace rather than dumped at once, because watching
    the agent decide is the point — but capped, because a long think reads as a
    hung page rather than as deliberation.
    """
    speed = min(max(speed, MIN_SPEED), MAX_SPEED)
    waits: list[float] = []
    previous_ms = 0.0
    for event in events:
        elapsed_ms = float(event["elapsed_ms"])
        # max(0, ...) guards a recording whose timings are not monotonic; a
        # negative sleep would be an error rather than an instant event.
        waits.append(max(min(elapsed_ms - previous_ms, max_gap_ms), 0.0) / 1000 / speed)
        previous_ms = elapsed_ms
    return waits


@dataclass(frozen=True)
class ReplayIndex:
    """The recordings on disk. A directory, not a cache: files change rarely."""

    directory: Path = REPLAY_DIR

    def path_for(self, scenario_id: str) -> Path:
        return self.directory / f"{scenario_id}.json"

    def ids(self) -> set[str]:
        if not self.directory.is_dir():
            return set()
        return {path.stem for path in self.directory.glob("*.json")}

    def load(self, scenario_id: str) -> Replay:
        path = self.path_for(scenario_id)
        if not path.is_file():
            raise ReplayNotFound(scenario_id)

        payload = json.loads(path.read_text())
        replay = parse_replay(payload, source=path.name)
        if replay.scenario_id != scenario_id:
            # Serving one scenario's run under another's name would caption a
            # real trace with the wrong customer, which is worse than a 404.
            raise ReplayFileError(
                f"{path.name}: records scenario {replay.scenario_id!r}, not {scenario_id!r}"
            )
        return replay

    def meta(self, scenario_id: str) -> dict[str, Any]:
        """Replay metadata for the page, or an explicit absence."""
        try:
            return self.load(scenario_id).meta()
        except (ReplayNotFound, ReplayFileError) as exc:
            return {"available": False, "reason": str(exc) or "not recorded yet"}


def write_replay(payload: dict[str, Any], *, index: ReplayIndex | None = None) -> Path:
    index = index or ReplayIndex()
    index.directory.mkdir(parents=True, exist_ok=True)
    path = index.path_for(payload["scenario_id"])
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path
