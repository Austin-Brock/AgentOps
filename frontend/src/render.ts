/**
 * Turning a trace into rows a person can read.
 *
 * All pure, so the interesting decisions are tested without a browser. The
 * decisions worth knowing about:
 *
 * - A refused call is rendered, never hidden. It is the most interesting thing
 *   the agent does and the least visible from the outside.
 * - An unknown event kind renders generically rather than being dropped. Phase 7
 *   will add kinds this build has never heard of, and a page that silently
 *   discards them would under-report what the agent did.
 * - Tool results are summarized to one line each, with the full payload kept on
 *   the row so the UI can offer it. A 4,000-character policy document pasted
 *   into the trace buries the trace.
 */

import type {
  ModelRespondedData,
  RequestReceivedData,
  RunFinishedData,
  ToolOutcomeData,
  TraceEvent,
  Usage,
} from "./wire";

export type RowTone = "request" | "decision" | "result" | "refused" | "failed" | "final";

export interface Row {
  key: string;
  elapsed: string;
  label: string;
  /** The one-line human summary. */
  text: string;
  /** Longer supporting text: a quoted message, an error explanation, the reply. */
  body?: string;
  tone: RowTone;
  /** The untouched event, for a raw view. */
  event: TraceEvent;
}

/** `22070.5` -> `"22.0"`. Seconds and tenths, matching the terminal trace. */
export function formatElapsed(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const tenths = Math.floor((ms % 1000) / 100);
  return `${String(seconds).padStart(2, "0")}.${tenths}`;
}

export function formatMoney(cents: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(cents / 100);
}

export function formatTokens(usage: Usage): string {
  const n = (value: number) => value.toLocaleString("en-US");
  return `${n(usage.input_tokens)} in / ${n(usage.output_tokens)} out`;
}

/**
 * `{order_id: "4471"}` -> `{ order_id: "4471" }`.
 *
 * Bounded rather than guaranteed to one line: a `customer_note` the model wrote
 * can run to hundreds of characters, and the limit stops that flooding the
 * trace. A long-but-reasonable argument list is still allowed to wrap.
 */
export function formatArguments(args: Record<string, unknown>, limit = 72): string {
  const entries = Object.entries(args);
  if (entries.length === 0) return "{}";
  const rendered = entries
    .map(([key, value]) => `${key}: ${typeof value === "string" ? `"${value}"` : String(value)}`)
    .join(", ");
  const body = rendered.length > limit ? `${rendered.slice(0, limit - 1)}…` : rendered;
  return `{ ${body} }`;
}

/**
 * One line describing what a tool came back with.
 *
 * Tool-specific on purpose: "delivered 2026-10-06 · $42.99 · no prior refunds"
 * is the sentence a reader wants, and no generic formatter produces it. The
 * fallback handles tools this build has not been taught, so a new tool degrades
 * to something readable instead of nothing.
 */
export function summarizeResult(tool: string, result: Record<string, unknown>): string {
  if (result.error) {
    return String(result.message ?? result.error);
  }

  switch (tool) {
    case "lookup_order": {
      const refunds = (result.existing_refunds as unknown[] | undefined) ?? [];
      const parts = [
        // The delivery date subsumes a "delivered" status; printing both read
        // as "delivered · delivered 2026-10-06". An undelivered order has no
        // date, so its status is the informative half.
        result.delivered_at
          ? `delivered ${result.delivered_at}`
          : String(result.status ?? "unknown status").replace(/_/g, " "),
        formatMoney(Number(result.total_cents ?? 0), String(result.currency ?? "USD")),
        refunds.length === 0 ? "no prior refunds" : `${refunds.length} prior refund(s)`,
      ];
      return parts.join(" · ");
    }
    case "get_refund_policy": {
      const chars = String(result.content ?? "").length;
      return `${result.document ?? "policy"} · ${chars.toLocaleString("en-US")} characters read`;
    }
    case "create_refund_request": {
      const parts = [
        String(result.refund_id ?? "refund"),
        formatMoney(Number(result.amount_cents ?? 0)),
        String(result.status ?? "unknown"),
      ];
      if (result.deduplicated) parts.push("already existed");
      return parts.join(" · ");
    }
    default:
      return summarizeUnknown(result);
  }
}

/** Scalar fields only: enough to be informative, short enough to stay one line. */
function summarizeUnknown(result: Record<string, unknown>): string {
  const scalars = Object.entries(result)
    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${String(value).slice(0, 40)}`);
  return scalars.length > 0 ? scalars.join(" · ") : "(no summary available)";
}

/**
 * The trace, as rows.
 *
 * `tool_requested` is dropped: it carries no payload, and the arguments the
 * model chose already arrived on the preceding `model_responded`. Keeping it
 * would put an empty row between every decision and its result.
 */
export function toRows(events: TraceEvent[]): Row[] {
  const rows: Row[] = [];

  for (const event of events) {
    const elapsed = formatElapsed(event.elapsed_ms);
    const base = { elapsed, event };

    switch (event.kind) {
      case "request_received": {
        const data = event.data as unknown as RequestReceivedData;
        rows.push({
          ...base,
          key: `${event.seq}`,
          label: "request received",
          text: `${data.tools_offered.length} tools offered`,
          body: data.goal,
          tone: "request",
        });
        break;
      }

      case "model_responded": {
        const data = event.data as unknown as ModelRespondedData;
        if (data.tool_calls.length === 0) {
          // No tools requested: this turn is the answer.
          rows.push({
            ...base,
            key: `${event.seq}`,
            label: "final reply to customer",
            text: `${data.model} · ${formatTokens(data.usage)}`,
            body: data.text,
            tone: "final",
          });
          break;
        }
        data.tool_calls.forEach((call, index) => {
          rows.push({
            ...base,
            key: `${event.seq}-${index}`,
            label: "model chose",
            text: `${call.tool} ${formatArguments(call.arguments)}`,
            // Preamble text the model wrote before reaching for the tool.
            body: index === 0 && data.text ? data.text : undefined,
            tone: "decision",
          });
        });
        break;
      }

      case "tool_requested":
        break;

      case "tool_succeeded":
      case "tool_failed":
      case "tool_refused": {
        const data = event.data as unknown as ToolOutcomeData;
        const refused = event.kind === "tool_refused";
        rows.push({
          ...base,
          key: `${event.seq}`,
          label: refused ? "refused" : event.kind === "tool_failed" ? "failed" : "returned",
          text: summarizeResult(data.tool, data.result),
          body: refused ? refusalReason(data) : undefined,
          tone: refused ? "refused" : event.kind === "tool_failed" ? "failed" : "result",
        });
        break;
      }

      case "run_finished":
        // The ending fills the summary bar rather than adding a row.
        break;

      default:
        rows.push({
          ...base,
          key: `${event.seq}`,
          label: event.kind,
          text: summarizeUnknown(event.detail),
          tone: "result",
        });
    }
  }

  return rows;
}

/** Why a call was refused, in the words the agent itself was given. */
function refusalReason(data: ToolOutcomeData): string {
  const missing = (data.result.missing_scopes as string[] | undefined) ?? [];
  const code = data.error_code ?? "refused";
  return missing.length > 0 ? `${code}: missing scope ${missing.join(", ")}` : code;
}

export interface Summary {
  status: string;
  toolsUsed: number;
  toolsRefused: number;
  usage: Usage;
  finalText: string;
}

/** The summary bar, from the terminal event. Absent until the run ends. */
export function toSummary(events: TraceEvent[]): Summary | null {
  const finished = events.find((event) => event.kind === "run_finished");
  if (!finished) return null;
  const data = finished.data as unknown as RunFinishedData;
  return {
    status: data.status,
    toolsUsed: data.tools_executed.length,
    toolsRefused: data.tools_refused.length,
    usage: data.usage,
    finalText: data.final_text,
  };
}
