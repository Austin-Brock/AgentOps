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
import type { Catalog, TraceEvent } from "./wire";

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

  it("offers no live button yet, and says why the trace is empty", async () => {
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

  it("says the run is recorded rather than implying it is live", async () => {
    render(<App />);
    expect(await screen.findByText(/A recorded run of the real agent/)).toBeTruthy();
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
