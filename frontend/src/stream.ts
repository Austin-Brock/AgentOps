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

import type { Catalog, TraceEvent } from "./wire";

const TERMINAL_KIND = "run_finished";

export interface StreamHandlers {
  onEvent: (event: TraceEvent) => void;
  onDone: () => void;
  onError: (message: string) => void;
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
