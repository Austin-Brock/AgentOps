/**
 * The page, mounted and driven by the real recording.
 *
 * The acceptance criterion this stands in for is "a visitor with no context can
 * understand what happened". That cannot be fully asserted, but the parts it
 * depends on can: the persona and order facts are on screen, the expectation is
 * stated before the run, the trace arrives progressively, a refused call is
 * visibly different, and the result is reported from the trace rather than from
 * the label.
 *
 * `EventSource` does not exist in jsdom, so it is replaced with a fake that
 * hands over the recorded events on demand. That also makes the progressive
 * behaviour testable: the test controls when each event arrives.
 */

import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { recordedEvents } from "./testing/recording";
import type { Catalog, LiveStatus, TraceEvent } from "./wire";

const CATALOG: Catalog = {
  version: 1,
  simulated_today: "2026-10-12",
  scenarios: [
    {
      id: "S01",
      title: "Valid damaged refund under the automatic limit",
      headline: "Mug arrived cracked",
      message: "My order #4471 arrived damaged. I'd like a refund.",
      customer: { id: "C-1001", name: "Dana Whitfield", tier: "standard" },
      order: {
        id: "4471",
        status: "delivered",
        total_cents: 4299,
        currency: "USD",
        placed_at: "2026-10-02",
        delivered_at: "2026-10-06",
        delivered_days_ago: 6,
        items: [{ name: "Stoneware Mug, 12oz", quantity: 2 }],
        prior_refunds: 0,
      },
      expected: {
        outcome: "Refund approved automatically",
        why: "$42.99 is inside both the 30-day window and the $50 automatic limit",
      },
      replay: {
        available: true,
        recorded_at: "2026-10-13T03:08:19+00:00",
        git_sha: "2b210e82b01d",
        git_dirty: true,
        provider: "openai",
        model: "gpt-5",
        model_version: "gpt-5-2025-08-07",
        events: 12,
        duration_ms: 22070.5,
      },
    },
  ],
};

/** A hand-driven EventSource: the test decides when each event lands. */
class FakeEventSource {
  static last: FakeEventSource | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.last = this;
  }

  close() {
    this.closed = true;
  }

  emit(event: TraceEvent) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
}

function mockCatalog(catalog: Catalog = CATALOG) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, json: async () => catalog })),
  );
}

const LIVE_ON: LiveStatus = {
  available: true,
  reason: null,
  message: null,
  runs_per_hour: 3,
  runs_remaining_this_hour: 2,
  daily_token_budget: 120000,
  tokens_remaining_today: 106000,
  budget_scope: "per server instance",
};

/** One SSE frame, in the shape the server actually writes. */
function frame(payload: unknown, event?: string): string {
  const head = event ? `event: ${event}\n` : "";
  return `${head}data: ${JSON.stringify(payload)}\n\n`;
}

/**
 * A server that answers both halves of a live run.
 *
 * `streamLive` reads a POST body rather than using `EventSource`, so the fake
 * has to be a real `ReadableStream` — which is also what makes the chunk
 * boundaries worth testing: a frame can be split anywhere, including mid-JSON.
 */
function mockServer({
  catalog = CATALOG,
  chunks = [],
  status = 200,
}: {
  catalog?: Catalog;
  chunks?: string[];
  status?: number;
}) {
  const calls: { url: string; init?: RequestInit }[] = [];

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (init?.method !== "POST") {
        return { ok: true, status: 200, json: async () => catalog };
      }
      const encoder = new TextEncoder();
      return {
        ok: status === 200,
        status,
        body:
          status === 200
            ? new ReadableStream<Uint8Array>({
                start(controller) {
                  for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
                  controller.close();
                },
              })
            : null,
      };
    }),
  );

  return calls;
}

const FINISHED = {
  seq: 1,
  kind: "run_finished",
  elapsed_ms: 1200,
  detail: { status: "completed", steps: 1 },
  data: {
    run_id: "agent-run-test",
    status: "completed",
    final_text: "Done.",
    steps: 1,
    tools_executed: ["lookup_order"],
    tools_refused: [],
    usage: { input_tokens: 4000, output_tokens: 2000 },
  },
};

async function playTo(kind: string) {
  const source = FakeEventSource.last;
  if (!source) throw new Error("the page never opened a stream");
  for (const event of recordedEvents) {
    await act(async () => source.emit(event));
    if (event.kind === kind) return source;
  }
  return source;
}

beforeEach(() => {
  mockCatalog();
  vi.stubGlobal("EventSource", FakeEventSource);
  FakeEventSource.last = null;
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("before the run", () => {
  it("shows who the customer is and what they actually ordered", async () => {
    const { container } = render(<App />);
    expect(await screen.findByText(/Dana Whitfield · order #4471/)).toBeTruthy();

    // Scoped to the facts line: "$42.99" also appears in the expectation
    // below it, which is a different claim about the same number.
    const facts = container.querySelector(".facts")?.textContent ?? "";
    expect(facts).toContain("$42.99");
    expect(facts).toContain("delivered 6 days ago");
    expect(facts).toContain("Stoneware Mug, 12oz ×2");
  });

  it("shows the customer's own words", async () => {
    render(<App />);
    expect(
      await screen.findByText("My order #4471 arrived damaged. I'd like a refund."),
    ).toBeTruthy();
  });

  it("states the expectation before the run, not after it", async () => {
    render(<App />);
    expect(await screen.findByText("expected")).toBeTruthy();
    expect(screen.getByText("Refund approved automatically")).toBeTruthy();
    expect(screen.getByText(/\$50 automatic limit/)).toBeTruthy();
    // And no verdict yet: nothing has happened to report.
    expect(screen.queryByText("actual")).toBeNull();
  });

  it("says when the recording was captured and by which model", async () => {
    render(<App />);
    const capture = await screen.findByText(/Recorded 2026-10-13/);
    // The precise version that answered, not the alias that was asked for.
    expect(capture.textContent).toContain("gpt-5-2025-08-07");
    expect(capture.textContent).toContain("2b210e82b01d");
  });

  it("offers no live button when the server does not say live is available", async () => {
    // This catalog has no `live` field at all, which is what an older server
    // sends. Absent is treated as unavailable: offering a paid button on a
    // guess is the wrong way round.
    render(<App />);
    const live = await screen.findByRole("button", { name: "Run live" });
    expect(live.getAttribute("disabled")).not.toBeNull();
    expect(screen.getByText(/Press Replay to watch/)).toBeTruthy();
  });
});

describe("the colophon", () => {
  it("says what the project is and links to the source", async () => {
    render(<App />);
    expect(await screen.findByText(/without an agent framework/)).toBeTruthy();
    expect(screen.getByText(/enforced inside the refund tool/)).toBeTruthy();
    const link = screen.getByRole("link", { name: /Source, design decisions/ });
    expect(link.getAttribute("href")).toContain("github.com/Austin-Brock/AgentOps");
  });

  it("says a replay is recorded rather than implying it is live", async () => {
    render(<App />);
    // Scoped to the footer: the card says the same thing above it, which is
    // the point — the distinction is stated wherever a visitor might miss it.
    const colophon = within(await screen.findByRole("contentinfo"));
    expect(colophon.getByText(/a recorded run of the real agent/)).toBeTruthy();
    expect(colophon.getByText(/calls the model now/)).toBeTruthy();
  });
});

describe("during the run", () => {
  it("asks the server for this scenario by id, never by message", async () => {
    render(<App />);
    (await screen.findByRole("button", { name: "Replay" })).click();
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());

    const url = FakeEventSource.last!.url;
    expect(url).toBe("/demo/scenarios/S01/replay");
    expect(url).not.toContain("arrived damaged");
  });

  it("shows each decision as it arrives, not all at the end", async () => {
    render(<App />);
    (await screen.findByRole("button", { name: "Replay" })).click();
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());

    await playTo("tool_succeeded");
    expect(screen.getByText(/lookup_order \{ order_id: "4471" \}/)).toBeTruthy();
    // The run is not over, so nothing claims it is.
    expect(screen.queryByText(/final reply to customer/)).toBeNull();
    expect(screen.queryByText("actual")).toBeNull();

    await playTo("run_finished");
    expect(screen.getByText(/create_refund_request/)).toBeTruthy();
    expect(screen.getByText(/final reply to customer/)).toBeTruthy();
  });

  it("closes the stream when the run finishes, so it cannot replay forever", async () => {
    render(<App />);
    (await screen.findByRole("button", { name: "Replay" })).click();
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());

    const source = await playTo("run_finished");
    expect(source.closed).toBe(true);
  });
});

describe("after the run", () => {
  it("reports what happened from the trace, beside what was expected", async () => {
    const { container } = render(<App />);
    (await screen.findByRole("button", { name: "Replay" })).click();
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());
    await playTo("run_finished");

    const summary = container.querySelector(".summary")?.textContent ?? "";
    // Counts, tokens and status all come from the run_finished event, not from
    // the expectation stated before the run.
    expect(summary).toContain("3 tools used");
    expect(summary).toContain("0 refused");
    expect(summary).toContain("6,206 in / 2,167 out");
    // And the two claims sit side by side for comparison, stated once each.
    expect(summary).toContain("run completed — expected refund approved automatically");
  });

  it("shows the reply the customer would actually receive", async () => {
    render(<App />);
    (await screen.findByRole("button", { name: "Replay" })).click();
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());
    await playTo("run_finished");

    const final = screen.getByText(/final reply to customer/).closest(".row");
    expect(within(final as HTMLElement).getByText(/RF-0002/)).toBeTruthy();
  });
});

describe("the refusal", () => {
  it("renders in its own tone with the scope that was missing", async () => {
    render(<App />);
    (await screen.findByRole("button", { name: "Replay" })).click();
    await waitFor(() => expect(FakeEventSource.last).not.toBeNull());

    const refusal: TraceEvent = {
      seq: 100,
      kind: "tool_refused",
      elapsed_ms: 2000,
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
    await act(async () => FakeEventSource.last!.emit(refusal));

    const row = screen.getByText(/missing scope: finance:write/).closest(".row");
    // The refusal is the feature, so it must not look like every other row.
    expect(row?.className).toContain("row-refused");
  });
});

describe("when a scenario has no recording", () => {
  it("says so instead of offering a dead button", async () => {
    const withoutReplay: Catalog = {
      ...CATALOG,
      scenarios: [
        {
          ...CATALOG.scenarios[0]!,
          replay: { available: false, reason: "not recorded yet" },
        },
      ],
    };
    mockCatalog(withoutReplay);
    render(<App />);

    expect(await screen.findByText(/No recording yet/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Replay" }).getAttribute("disabled")).not.toBeNull();
  });
});

describe("when the backend is unreachable", () => {
  it("says what to check rather than showing a blank page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 503, json: async () => ({}) })),
    );
    render(<App />);
    expect(await screen.findByText(/Is the backend running on port 8000/)).toBeTruthy();
  });
});

/**
 * Live mode — step 6.
 *
 * The page's job here is narrow but easy to get wrong: offer the button only
 * when the server says it is available, send nothing but a scenario id, and be
 * honest about which mode produced the trace on screen. A page that labels a
 * degraded run "live" is worse than one with no live button at all.
 */
describe("live mode", () => {
  it("offers the button when the server says live is available", async () => {
    mockServer({ catalog: { ...CATALOG, live: LIVE_ON } });
    render(<App />);

    const live = await screen.findByRole("button", { name: "Run live" });
    expect(live.getAttribute("disabled")).toBeNull();
  });

  it("explains why the button is off instead of just disabling it", async () => {
    const message = "The demo's daily token budget is spent, so this is a recorded run.";
    mockServer({
      catalog: {
        ...CATALOG,
        live: { ...LIVE_ON, available: false, reason: "budget_exhausted", message },
      },
    });
    render(<App />);

    const live = await screen.findByRole("button", { name: "Run live" });
    expect(live.getAttribute("disabled")).not.toBeNull();
    // A disabled control with no reason reads as a broken page, not a budget.
    expect(live.getAttribute("title")).toBe(message);
  });

  it("posts the scenario id and never a message", async () => {
    const calls = mockServer({
      catalog: { ...CATALOG, live: LIVE_ON },
      chunks: [frame({ mode: "live", reason: null, message: null }, "notice"), frame(FINISHED)],
    });
    render(<App />);

    // Found before the act() block: resolving the catalog is what renders the
    // button, and looking for it inside the same act() races that render.
    const live = await screen.findByRole("button", { name: "Run live" });
    await act(async () => live.click());

    const post = calls.find((call) => call.init?.method === "POST");
    expect(post?.url).toBe("/demo/scenarios/S01/run");
    // The prompt is resolved server-side from the dataset (DD-011): there is
    // no body for a crafted message to travel in.
    expect(post?.init?.body).toBeUndefined();
  });

  it("says the trace ran live when it did", async () => {
    mockServer({
      catalog: { ...CATALOG, live: LIVE_ON },
      chunks: [frame({ mode: "live", reason: null, message: null }, "notice"), frame(FINISHED)],
    });
    render(<App />);

    // Found before the act() block: resolving the catalog is what renders the
    // button, and looking for it inside the same act() races that render.
    const live = await screen.findByRole("button", { name: "Run live" });
    await act(async () => live.click());

    expect(await screen.findByText("ran live just now")).toBeTruthy();
  });

  it("relabels the trace and shows the notice when a control degrades the run", async () => {
    const message =
      "The demo's daily token budget is spent, so this is a recorded run of the " +
      "same scenario against the real model. Live runs resume tomorrow.";
    mockServer({
      catalog: { ...CATALOG, live: LIVE_ON },
      chunks: [
        frame({ mode: "replay", reason: "budget_exhausted", message }, "notice"),
        frame(FINISHED),
      ],
    });
    render(<App />);

    // Found before the act() block: resolving the catalog is what renders the
    // button, and looking for it inside the same act() races that render.
    const live = await screen.findByRole("button", { name: "Run live" });
    await act(async () => live.click());

    // The server's own words, not a code the page guessed a sentence from.
    expect(await screen.findByText(message)).toBeTruthy();
    // And the summary does not claim a live run happened.
    expect(screen.getByText(/recorded run \(live was unavailable\)/)).toBeTruthy();
    expect(screen.queryByText("ran live just now")).toBeNull();
  });

  it("reassembles frames split across chunk boundaries", async () => {
    // A read can land anywhere, including inside the JSON. Splitting one frame
    // mid-object is the case a naive per-chunk parser gets wrong.
    const whole = frame(FINISHED);
    const cut = Math.floor(whole.length / 2);
    mockServer({
      catalog: { ...CATALOG, live: LIVE_ON },
      chunks: [
        frame({ mode: "live", reason: null, message: null }, "notice"),
        whole.slice(0, cut),
        whole.slice(cut),
      ],
    });
    render(<App />);

    // Found before the act() block: resolving the catalog is what renders the
    // button, and looking for it inside the same act() races that render.
    const live = await screen.findByRole("button", { name: "Run live" });
    await act(async () => live.click());

    expect(await screen.findByText("ran live just now")).toBeTruthy();
    // The split frame's payload survived, not just its arrival: the summary is
    // built from the terminal event's data.
    const summary = (await screen.findByText(/tools used/)).closest(".summary");
    expect(summary?.textContent).toContain("1");
    expect(summary?.textContent).toContain("4,000 in / 2,000 out");
  });

  it("reports a run that died mid-stream without stranding the page", async () => {
    const message = "The live run stopped before it finished — the model provider failed.";
    mockServer({
      catalog: { ...CATALOG, live: LIVE_ON },
      chunks: [
        frame({ mode: "live", reason: null, message: null }, "notice"),
        frame({ mode: "live", reason: "run_failed", message }, "notice"),
        frame({ ...FINISHED, data: { ...FINISHED.data, status: "provider_error" } }),
      ],
    });
    render(<App />);

    // Found before the act() block: resolving the catalog is what renders the
    // button, and looking for it inside the same act() races that render.
    const live = await screen.findByRole("button", { name: "Run live" });
    await act(async () => live.click());

    expect(await screen.findByText(message)).toBeTruthy();
    // The synthesized ending still ends the run, so the page is not left
    // showing "thinking…" forever.
    expect(screen.queryByText("thinking")).toBeNull();
  });

  it("discloses that the daily budget is counted per instance", async () => {
    // The honest limitation, on screen rather than only in a design document:
    // the counters are per process, so several instances raise the real cap.
    mockServer({ catalog: { ...CATALOG, live: LIVE_ON } });
    render(<App />);

    const allowance = await screen.findByText(/Daily budget/);
    expect(allowance.textContent).toContain("per server instance");
    expect(allowance.textContent).toContain("106,000");
    expect(allowance.textContent).toContain("higher real ceiling");
  });
});
