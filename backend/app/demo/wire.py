"""The event wire format, defined once.

Four consumers need the same bytes: the replay recorder that writes a run to
disk, the replay endpoint that serves it back, the live endpoint that streams a
run as it happens, and the frontend that draws all of it. Three hand-rolled
encodings of one event is the reliable way to make a replay stop matching a
live run, so there is one encoder here and everything else calls it.

`TraceEvent` already carries the two halves this needs (DD-012): `detail` for
the compact log line and `data` for the structures a page draws. Encoding is
therefore mechanical — which is the point. Nothing here decides anything.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from app.observability.trace import TraceEvent

WIRE_VERSION = 1


def encode_event(event: TraceEvent, seq: int) -> dict[str, Any]:
    """One event, as the page receives it.

    `seq` is the event's position in the run. The client uses it to order and
    de-duplicate; SSE carries it as the frame id, which is also what makes a
    `Last-Event-ID` resume possible later without changing the format.
    """
    return {
        "seq": seq,
        "kind": event.kind,
        # Milliseconds since the run began, rounded: sub-microsecond precision
        # is noise in a file that exists to be replayed at human speed.
        "elapsed_ms": round(event.elapsed_ms, 1),
        "detail": event.detail,
        "data": event.data,
    }


def encode_events(events: Iterable[TraceEvent]) -> list[dict[str, Any]]:
    """A whole run's events, numbered from zero."""
    return [encode_event(event, seq) for seq, event in enumerate(events)]


def sse_frame(payload: dict[str, Any]) -> str:
    """One Server-Sent Events frame.

    Deliberately no `event:` field. Naming an SSE event means `onmessage` never
    fires for it and the client must register a listener per kind — and the kinds
    are the one thing in this format that will keep growing. The kind travels
    inside the payload instead, so a client written today still receives an
    event kind added in Phase 7.
    """
    body = json.dumps(payload, separators=(",", ":"))
    return f"id: {payload['seq']}\ndata: {body}\n\n"


def sse_comment(text: str) -> str:
    """A comment frame. Ignored by clients, but it keeps a connection warm."""
    return f": {text}\n\n"


#: The SSE event name for demo control frames — which mode a request got, and
#: why it is not the one that was asked for.
NOTICE_EVENT = "notice"


def sse_notice(payload: dict[str, Any]) -> str:
    """One control frame, on its own named SSE channel.

    `sse_frame` deliberately leaves the event unnamed so that trace kinds added
    in a later phase still reach a client's `onmessage`. That argument is about
    the trace, whose kinds keep growing. A notice is the opposite kind of thing:
    a fixed, single-purpose control channel that says which mode the caller got.

    Naming it is what keeps the two apart. A notice is not a trace event — it
    has no place in the run, no `seq`, and belongs in the page's chrome rather
    than in the list of what the agent did — so a client that routed it through
    `onmessage` would have to filter it back out of its own trace by kind, and
    a recorder that received one would have to know not to write it down.
    """
    body = json.dumps(payload, separators=(",", ":"))
    return f"event: {NOTICE_EVENT}\ndata: {body}\n\n"
