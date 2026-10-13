/**
 * The server's wire format, mirrored.
 *
 * These types are the frontend half of `backend/app/demo/wire.py`. They are
 * hand-written rather than generated because there are nine of them and a
 * codegen step is a worse trade than a review when the format changes.
 *
 * Every event's `detail` is the compact log line and `data` is what a page
 * draws, which is why only `data` is typed per kind here.
 */

export interface Usage {
  input_tokens: number;
  output_tokens: number;
}

export interface ToolCallDecision {
  tool_use_id: string;
  tool: string;
  arguments: Record<string, unknown>;
}

/** The kinds the trace emits. Unknown kinds are rendered generically, never dropped. */
export type EventKind =
  | "request_received"
  | "model_responded"
  | "tool_requested"
  | "tool_succeeded"
  | "tool_failed"
  | "tool_refused"
  | "run_finished";

export interface RequestReceivedData {
  goal: string;
  customer_id: string | null;
  tools_offered: string[];
}

export interface ModelRespondedData {
  step: number;
  stop_reason: string;
  text: string;
  model: string;
  tool_calls: ToolCallDecision[];
  usage: Usage;
}

export interface ToolOutcomeData {
  tool: string;
  tool_use_id: string;
  arguments: Record<string, unknown>;
  executed: boolean;
  is_error: boolean;
  error_code: string | null;
  result: Record<string, unknown>;
  duration_ms: number;
}

export interface RunFinishedData {
  run_id: string;
  status: string;
  final_text: string;
  steps: number;
  tools_executed: string[];
  tools_refused: string[];
  usage: Usage;
}

export interface TraceEvent {
  seq: number;
  kind: EventKind | string;
  elapsed_ms: number;
  detail: Record<string, unknown>;
  data: Record<string, unknown>;
}

// --- the scenario catalog ---------------------------------------------------

export interface Customer {
  id: string;
  name: string;
  tier: string;
}

export interface OrderItem {
  name: string;
  quantity: number;
}

export interface Order {
  id: string;
  status: string;
  total_cents: number;
  currency: string;
  placed_at: string;
  delivered_at: string | null;
  delivered_days_ago: number | null;
  items: OrderItem[];
  prior_refunds: number;
}

/** What the evaluation suite requires, stated before the run rather than after. */
export interface Expectation {
  outcome: string;
  why: string;
}

/**
 * When the recording was captured, so a stale replay is visible rather than
 * invisible. `available: false` is a real state: a scenario with no recording
 * yet, which the page says plainly instead of offering a dead button.
 */
export type ReplayMeta =
  | {
      available: true;
      recorded_at: string;
      git_sha: string;
      git_dirty: boolean;
      provider: string;
      /** The alias that was requested, e.g. `gpt-5`. */
      model: string;
      /**
       * What the provider reported answering, e.g. `gpt-5-2025-08-07`. Optional
       * because a server older than this build does not send it, and a page
       * that renders an empty segment there reads as a bug.
       */
      model_version?: string;
      events: number;
      duration_ms: number;
    }
  | { available: false; reason: string };

export interface Scenario {
  id: string;
  title: string;
  headline: string;
  message: string;
  customer: Customer;
  order: Order;
  expected: Expectation;
  replay: ReplayMeta;
}

export interface Catalog {
  version: number;
  /** Business time is simulated against a fixed date, so the page shows it. */
  simulated_today: string;
  scenarios: Scenario[];
}
