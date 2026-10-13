/**
 * Reading the replay stream.
 *
 * One behaviour here is not optional. **`EventSource` reconnects when the server
 * closes the stream**, and a finished replay is a closed stream — so without
 * closing it ourselves on `run_finished`, the replay restarts forever and the
 * page quietly hammers the endpoint. The subscription below closes on the
 * terminal event, which is reliable because every run records one on every exit
 * path (DD-012).
 *
 * Events carry their kind inside the payload rather than in the SSE `event:`
 * field, so there is a single `onmessage` listener here and new kinds arrive
 * without a code change.
 */

import type { Catalog, Notice, TraceEvent } from "./wire";

const TERMINAL_KIND = "run_finished";

/** The server's control channel, named so it never reaches the trace list. */
const NOTICE_EVENT = "notice";

export interface StreamHandlers {
  onEvent: (event: TraceEvent) => void;
  onDone: () => void;
  onError: (message: string) => void;
  /** A control frame: which mode the server granted, and why. */
  onNotice?: (notice: Notice) => void;
}

export interface Subscription {
  close: () => void;
}

export async function fetchCatalog(): Promise<Catalog> {
  const response = await fetch("/demo/scenarios");
  if (!response.ok) {
    throw new Error(`Could not load the scenario list (HTTP ${response.status}).`);
  }
  return (await response.json()) as Catalog;
}

/**
 * Stream a recorded run.
 *
 * `speed` is forwarded to the server, which owns the pacing: the client never
 * decides how fast a run appeared to happen.
 */
export function streamReplay(
  scenarioId: string,
  handlers: StreamHandlers,
  options: { speed?: number } = {},
): Subscription {
  const query = options.speed && options.speed !== 1 ? `?speed=${options.speed}` : "";
  const source = new EventSource(
    `/demo/scenarios/${encodeURIComponent(scenarioId)}/replay${query}`,
  );
  let finished = false;

  const close = () => {
    finished = true;
    source.close();
  };

  source.onmessage = (message) => {
    let event: TraceEvent;
    try {
      event = JSON.parse(message.data) as TraceEvent;
    } catch {
      handlers.onError("The server sent an event this page could not read.");
      close();
      return;
    }

    handlers.onEvent(event);
    if (event.kind === TERMINAL_KIND) {
      // The run is over. Close before the browser reconnects and replays it again.
      close();
      handlers.onDone();
    }
  };

  source.onerror = () => {
    if (finished) return;
    // EventSource reports a closed or failed connection the same way, and the
    // only case worth reporting is one where the run never reached its end.
    handlers.onError("The replay stream stopped before the run finished.");
    close();
  };

  return { close };
}

/**
 * Start a live run and read the stream back.
 *
 * Hand-rolled rather than `EventSource` because the endpoint is a POST, and
 * `EventSource` can only issue a GET. The endpoint is a POST deliberately: a GET
 * that spends money is one a prefetcher or an unfurler will eventually fire by
 * itself. Paying for that with ~40 lines of parser here is the right trade.
 *
 * The server always answers 200 and always ends the stream, so there is no
 * fallback logic in this function. A request that no control allowed comes back
 * as a `notice` frame followed by the recorded run — the page relabels itself
 * and the visitor still watches the agent work.
 */
export function streamLive(scenarioId: string, handlers: StreamHandlers): Subscription {
  const controller = new AbortController();
  let finished = false;

  const close = () => {
    finished = true;
    controller.abort();
  };

  const fail = (message: string) => {
    if (finished) return;
    finished = true;
    handlers.onError(message);
  };

  void (async () => {
    let response: Response;
    try {
      response = await fetch(`/demo/scenarios/${encodeURIComponent(scenarioId)}/run`, {
        method: "POST",
        signal: controller.signal,
        headers: { Accept: "text/event-stream" },
      });
    } catch {
      // An aborted request lands here too, and a deliberate close is not an error.
      if (!finished) fail("Could not reach the server to start a live run.");
      return;
    }

    if (!response.ok || !response.body) {
      fail(
        response.status === 404
          ? "That scenario is not on the demo's list."
          : `The server refused the live run (HTTP ${response.status}).`,
      );
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        // Frames are separated by a blank line. The last chunk is kept: a read
        // can split a frame anywhere, including mid-JSON.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          if (dispatch(frame, handlers)) {
            close();
            handlers.onDone();
            return;
          }
        }
      }
      // The body ended without a terminal event. Every run records one on every
      // exit path and the endpoint synthesizes one for a run that died, so this
      // is the connection dropping rather than the run ending.
      fail("The live stream stopped before the run finished.");
    } catch {
      if (!finished) fail("The live stream failed partway through the run.");
    }
  })();

  return { close };
}

/**
 * Parse one SSE frame and hand it to the right callback.
 *
 * Returns true when the frame ended the run. Comment-only frames — the server
 * sends one to flush a buffering proxy — carry no data and are skipped.
 */
function dispatch(frame: string, handlers: StreamHandlers): boolean {
  let event = "";
  const data: string[] = [];

  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (data.length === 0) return false;

  let payload: unknown;
  try {
    payload = JSON.parse(data.join("\n"));
  } catch {
    handlers.onError("The server sent an event this page could not read.");
    return true;
  }

  if (event === NOTICE_EVENT) {
    handlers.onNotice?.(payload as Notice);
    return false;
  }

  const traceEvent = payload as TraceEvent;
  handlers.onEvent(traceEvent);
  return traceEvent.kind === TERMINAL_KIND;
}
