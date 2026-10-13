/**
 * Rendering the trace — tested against the real recording, not a fixture.
 *
 * `data/demo/replays/S01.json` is one real gpt-5 run, so these tests fail if
 * the page stops being able to draw what the agent actually produces. A fixture
 * written here would only ever agree with the assumptions that wrote it.
 *
 * The refusal case has no recording yet — S10 arrives when the manifest widens
 * — so it is driven from a synthesized event whose shape comes from the
 * executor's own payload.
 */

import { describe, expect, it } from "vitest";

import {
  formatArguments,
  formatElapsed,
  formatMoney,
  formatTokens,
  summarizeResult,
  toRows,
  toSummary,
} from "./render";
import { recordedEvents, recording } from "./testing/recording";
import type { TraceEvent } from "./wire";

describe("formatting", () => {
  it("renders elapsed time as seconds and tenths", () => {
    expect(formatElapsed(0)).toBe("00.0");
    expect(formatElapsed(1_234)).toBe("01.2");
    expect(formatElapsed(22_070.5)).toBe("22.0");
  });

  it("renders cents as money", () => {
    expect(formatMoney(4299)).toBe("$42.99");
    expect(formatMoney(0)).toBe("$0.00");
  });

  it("renders usage the way the summary bar reads it", () => {
    expect(formatTokens({ input_tokens: 6206, output_tokens: 2167 })).toBe("6,206 in / 2,167 out");
  });

  it("renders arguments compactly and quotes strings", () => {
    expect(formatArguments({ order_id: "4471" })).toBe('{ order_id: "4471" }');
    expect(formatArguments({ amount_cents: 4299 })).toBe("{ amount_cents: 4299 }");
    expect(formatArguments({})).toBe("{}");
  });

  it("bounds argument length so a long note cannot flood the trace", () => {
    const long = formatArguments({ note: "x".repeat(300) });
    expect(long.length).toBeLessThan(90);
    expect(long).toContain("…");
  });
});

describe("summarizing real tool results", () => {
  const resultOf = (tool: string) => {
    const event = recordedEvents.find(
      (candidate) => candidate.kind === "tool_succeeded" && candidate.data.tool === tool,
    );
    if (!event) throw new Error(`the recording has no ${tool} result`);
    return event.data.result as Record<string, unknown>;
  };

  it("describes a looked-up order in one line", () => {
    expect(summarizeResult("lookup_order", resultOf("lookup_order"))).toBe(
      "delivered 2026-10-06 · $42.99 · no prior refunds",
    );
  });

  it("describes the policy read without pasting the document", () => {
    const summary = summarizeResult("get_refund_policy", resultOf("get_refund_policy"));
    expect(summary).toContain("refund_policy.md");
    expect(summary).toMatch(/characters read/);
    expect(summary.length).toBeLessThan(60);
  });

  it("describes a created refund with its reference and approval state", () => {
    expect(summarizeResult("create_refund_request", resultOf("create_refund_request"))).toBe(
      "RF-0002 · $42.99 · auto_approved",
    );
  });

  it("prefers a tool error's own message over a generic summary", () => {
    const summary = summarizeResult("create_refund_request", {
      error: "outside_refund_window",
      message: "Order 4472 was delivered 185 days ago, outside the 30-day refund window.",
    });
    expect(summary).toContain("185 days ago");
  });

  it("degrades readably for a tool it has never been taught", () => {
    const summary = summarizeResult("send_replacement", {
      shipment_id: "SH-9",
      carrier: "UPS",
    });
    expect(summary).toBe("shipment_id: SH-9 · carrier: UPS");
  });
});

describe("the trace as rows", () => {
  const rows = toRows(recordedEvents);

  it("opens with the request and shows what the customer wrote", () => {
    expect(rows[0]?.label).toBe("request received");
    expect(rows[0]?.body).toBe("My order #4471 arrived damaged. I'd like a refund.");
  });

  it("shows each decision the model made with its arguments", () => {
    const decisions = rows.filter((row) => row.tone === "decision");
    expect(decisions.map((row) => row.text)).toEqual([
      'lookup_order { order_id: "4471" }',
      "get_refund_policy {}",
      'create_refund_request { order_id: "4471", reason: "damaged_on_arrival", amount_cents: 4299, cus… }',
    ]);
  });

  it("pairs every decision with the result that came back", () => {
    const sequence = rows.map((row) => row.tone);
    expect(sequence).toEqual([
      "request",
      "decision",
      "result",
      "decision",
      "result",
      "decision",
      "result",
      "final",
    ]);
  });

  it("drops tool_requested, which carries no payload of its own", () => {
    expect(recordedEvents.some((event) => event.kind === "tool_requested")).toBe(true);
    expect(rows.some((row) => row.event.kind === "tool_requested")).toBe(false);
  });

  it("ends with the reply the customer would receive", () => {
    const last = rows[rows.length - 1];
    expect(last?.label).toBe("final reply to customer");
    expect(last?.body).toContain("RF-0002");
  });

  it("renders a refused call as its own tone, with the missing scope", () => {
    const refused: TraceEvent = {
      seq: 99,
      kind: "tool_refused",
      elapsed_ms: 1500,
      detail: { tool: "apply_account_credit", reason: "permission_denied" },
      data: {
        tool: "apply_account_credit",
        tool_use_id: "toolu_x",
        arguments: { customer_id: "C-1001", amount_cents: 4299 },
        executed: false,
        is_error: true,
        error_code: "permission_denied",
        result: {
          error: "permission_denied",
          message: "Not authorized to execute 'apply_account_credit'.",
          missing_scopes: ["finance:write"],
        },
        duration_ms: 0,
      },
    };

    const row = toRows([refused])[0];
    expect(row?.tone).toBe("refused");
    expect(row?.label).toBe("refused");
    expect(row?.body).toBe("permission_denied: missing scope finance:write");
  });

  it("renders an event kind it has never heard of rather than dropping it", () => {
    const future: TraceEvent = {
      seq: 42,
      kind: "memory_recalled",
      elapsed_ms: 100,
      detail: { episodes: 2, customer: "C-1001" },
      data: {},
    };
    const row = toRows([future])[0];
    expect(row?.label).toBe("memory_recalled");
    expect(row?.text).toContain("episodes: 2");
  });
});

describe("the summary bar", () => {
  it("is absent until the run ends, so it cannot report a partial run", () => {
    const partial = recordedEvents.filter((event) => event.kind !== "run_finished");
    expect(toSummary(partial)).toBeNull();
  });

  it("reports what the recorded run actually did", () => {
    const summary = toSummary(recordedEvents);
    expect(summary).not.toBeNull();
    expect(summary?.status).toBe("completed");
    expect(summary?.toolsUsed).toBe(3);
    expect(summary?.toolsRefused).toBe(0);
    expect(summary?.usage.input_tokens).toBeGreaterThan(0);
    expect(summary?.finalText).toContain("4471");
  });

  it("agrees with the recording's own summary fields", () => {
    expect(toSummary(recordedEvents)?.toolsUsed).toBe(recording.tools_executed.length);
    expect(toSummary(recordedEvents)?.status).toBe(recording.status);
  });
});
