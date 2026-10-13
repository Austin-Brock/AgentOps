/**
 * The real recording, loaded for tests.
 *
 * Tests read `data/demo/replays/S01.json` — one real gpt-5 run — rather than a
 * fixture written here, so they fail when the page stops being able to draw
 * what the agent actually produces.
 *
 * Resolved from the working directory rather than `import.meta.url`: under
 * jsdom that URL is an http:// address, not a file path.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { TraceEvent } from "../wire";

export interface Recording {
  scenario_id: string;
  status: string;
  final_text: string;
  tools_executed: string[];
  tools_refused: string[];
  events: TraceEvent[];
}

const PATH = resolve(process.cwd(), "../data/demo/replays/S01.json");

export const recording = JSON.parse(readFileSync(PATH, "utf8")) as Recording;
export const recordedEvents = recording.events;
