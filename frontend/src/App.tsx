/**
 * The demo page.
 *
 * Deliberately plain: the trace is the content, and chrome competes with it.
 * No state library either — a scenario picker and an event list do not need one.
 *
 * The page states the evaluation's **expected** outcome before the run and the
 * trace's **actual** outcome after it (DD-013). Several scenarios have more than
 * one legitimate path, so a pre-printed result would eventually contradict the
 * trace underneath it. A prediction being verified is also simply better than a
 * label: the visitor watches the claim get tested.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { formatElapsed, formatMoney, formatTokens, toRows, toSummary } from "./render";
import { fetchCatalog, streamReplay, type Subscription } from "./stream";
import type { Catalog, Scenario, TraceEvent } from "./wire";

type Status = "idle" | "playing" | "done" | "error";

export default function App() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [status, setStatus] = useState<Status>("idle");
  const [streamError, setStreamError] = useState<string | null>(null);
  const subscription = useRef<Subscription | null>(null);

  useEffect(() => {
    fetchCatalog()
      .then((loaded) => {
        setCatalog(loaded);
        setSelectedId(loaded.scenarios[0]?.id ?? null);
      })
      .catch((error: unknown) => setLoadError(String(error)));
  }, []);

  // A page left mid-replay must not leave a stream open behind it.
  useEffect(() => () => subscription.current?.close(), []);

  const selected = useMemo(
    () => catalog?.scenarios.find((scenario) => scenario.id === selectedId) ?? null,
    [catalog, selectedId],
  );

  const select = useCallback((id: string) => {
    subscription.current?.close();
    subscription.current = null;
    setSelectedId(id);
    setEvents([]);
    setStatus("idle");
    setStreamError(null);
  }, []);

  const play = useCallback(() => {
    if (!selected) return;
    subscription.current?.close();
    setEvents([]);
    setStreamError(null);
    setStatus("playing");
    subscription.current = streamReplay(selected.id, {
      onEvent: (event) => setEvents((previous) => [...previous, event]),
      onDone: () => setStatus("done"),
      onError: (message) => {
        setStreamError(message);
        setStatus("error");
      },
    });
  }, [selected]);

  if (loadError) {
    return (
      <main className="page">
        <Header simulatedToday={null} />
        <p className="notice error">
          The scenario list could not be loaded. Is the backend running on port 8000?
          <br />
          <code>{loadError}</code>
        </p>
      </main>
    );
  }

  if (!catalog || !selected) {
    return (
      <main className="page">
        <Header simulatedToday={null} />
        <p className="notice">Loading scenarios…</p>
      </main>
    );
  }

  return (
    <main className="page">
      <Header simulatedToday={catalog.simulated_today} />

      <section className="picker" aria-label="Pick a customer">
        {catalog.scenarios.map((scenario) => (
          <button
            key={scenario.id}
            type="button"
            className={scenario.id === selected.id ? "chip chip-on" : "chip"}
            aria-pressed={scenario.id === selected.id}
            onClick={() => select(scenario.id)}
          >
            <span className="chip-who">{scenario.customer.name.split(" ")[0]}</span>
            <span className="chip-what">{scenario.headline}</span>
          </button>
        ))}
      </section>

      <ScenarioCard
        scenario={selected}
        status={status}
        onPlay={play}
        simulatedToday={catalog.simulated_today}
      />

      {streamError && <p className="notice error">{streamError}</p>}

      <Trace events={events} status={status} />
      <SummaryBar events={events} scenario={selected} />
      <Colophon />
    </main>
  );
}

const REPO = "https://github.com/Austin-Brock/AgentOps";

/**
 * What the visitor is actually looking at.
 *
 * Without this the page explains each scenario well and never says why the
 * project exists. For anyone evaluating the work rather than the agent, this is
 * the most important text on it.
 */
function Colophon() {
  return (
    <footer className="colophon">
      <p>
        <strong>What this is.</strong> A customer support agent built without an agent framework —
        the loop, the tool schemas, the authorization check and the step ceiling are all ordinary
        Python you can read. Safety rules live in application code rather than in the prompt: the
        $50 automatic and $200 manager refund ceilings are enforced inside the refund tool, and the
        agent is deliberately offered a tool it has no scope to execute, so you can watch the denial
        happen.
      </p>
      <p>
        <strong>What you are watching.</strong> A recorded run of the real agent against{" "}
        <code>gpt-5</code>, replayed at the pace it happened. The scenarios come from the same
        evaluation dataset the test suite grades against, so the demo and the measurements cannot
        disagree. Nothing here is scripted: the agent chose each tool from what the previous result
        told it.
      </p>
      <p>
        <a href={REPO} target="_blank" rel="noreferrer">
          Source, design decisions, and the evaluation results →
        </a>
      </p>
    </footer>
  );
}

function Header({ simulatedToday }: { simulatedToday: string | null }) {
  return (
    <header className="header">
      <h1>Watch a support agent work</h1>
      <p>
        A customer writes in. The agent decides which tools to use, what the customer is entitled
        to, and what it is not allowed to do — tool call by tool call, including the calls it is
        refused.
      </p>
      {simulatedToday && (
        <p className="aside">
          Business data runs against a fixed date of <strong>{simulatedToday}</strong>, so refund
          windows do not drift as real time passes.
        </p>
      )}
    </header>
  );
}

function ScenarioCard({
  scenario,
  status,
  onPlay,
  simulatedToday,
}: {
  scenario: Scenario;
  status: Status;
  onPlay: () => void;
  simulatedToday: string;
}) {
  const { customer, order, expected, replay } = scenario;
  const age =
    order.delivered_days_ago === null
      ? `${order.status.replace("_", " ")}, not yet delivered`
      : `delivered ${order.delivered_days_ago} days ago`;

  return (
    <section className="card">
      <div className="card-head">
        <h2>
          {customer.name} · order #{order.id}
        </h2>
        <p className="facts">
          {formatMoney(order.total_cents, order.currency)} · {age} ·{" "}
          {order.items.map((item) => `${item.name} ×${item.quantity}`).join(", ")}
          {order.prior_refunds > 0 && ` · ${order.prior_refunds} refund already on this order`}
        </p>
      </div>

      <blockquote className="message">{scenario.message}</blockquote>

      <div className="expected">
        <span className="tag">expected</span>
        <span>
          <strong>{expected.outcome}</strong> — {expected.why}
        </span>
      </div>

      <div className="actions">
        <button
          type="button"
          className="primary"
          onClick={onPlay}
          disabled={status === "playing" || !replay.available}
        >
          {status === "playing" ? "Replaying…" : status === "done" ? "Replay again" : "Replay"}
        </button>
        <button type="button" className="secondary" disabled title="Arrives in step 6">
          Run live
        </button>
        {replay.available ? (
          <p className="capture">
            Recorded {replay.recorded_at.slice(0, 10)} · {replay.model_version || replay.model} ·{" "}
            {replay.git_sha}
            {replay.git_dirty && " +dirty"} · ran in {(replay.duration_ms / 1000).toFixed(1)}s,
            replayed faster
          </p>
        ) : (
          <p className="capture">
            No recording yet — run <code>make record-replays</code>.
          </p>
        )}
      </div>

      <p className="aside">
        This is a recorded run of the real agent, not a mock. The message is resolved on the server
        from the evaluation dataset; this page only sends a scenario id. Dates are relative to{" "}
        {simulatedToday}.
      </p>
    </section>
  );
}

function Trace({ events, status }: { events: TraceEvent[]; status: Status }) {
  const rows = useMemo(() => toRows(events), [events]);

  if (status === "idle") {
    return (
      <section className="trace trace-empty">
        <p>Press Replay to watch the agent work through this one.</p>
      </section>
    );
  }

  return (
    <section className="trace" aria-live="polite">
      {rows.map((row) => (
        <article key={row.key} className={`row row-${row.tone}`}>
          <span className="clock">{row.elapsed}</span>
          <span className="label">{row.label}</span>
          <div className="content">
            <span className="text">{row.text}</span>
            {row.body && <p className="body">{row.body}</p>}
          </div>
        </article>
      ))}
      {status === "playing" && (
        <article className="row row-pending">
          <span className="clock">
            {events.length > 0 ? formatElapsed(events[events.length - 1]!.elapsed_ms) : "00.0"}
          </span>
          <span className="label">thinking</span>
          <div className="content">
            <span className="text">…</span>
          </div>
        </article>
      )}
    </section>
  );
}

function SummaryBar({ events, scenario }: { events: TraceEvent[]; scenario: Scenario }) {
  const summary = useMemo(() => toSummary(events), [events]);
  if (!summary) return null;

  return (
    <section className="summary">
      <div className="summary-line">
        <span>
          <strong>{summary.toolsUsed}</strong> tools used
        </span>
        <span className={summary.toolsRefused > 0 ? "refused-count" : undefined}>
          <strong>{summary.toolsRefused}</strong> refused
        </span>
        <span>{formatTokens(summary.usage)}</span>
      </div>
      <div className="verdict">
        <span className="tag">actual</span>
        <span>
          {describeActual(summary.toolsRefused, summary.status)} — expected{" "}
          <em>{scenario.expected.outcome.toLowerCase()}</em>
        </span>
      </div>
    </section>
  );
}

/**
 * What the run actually did, in the same register as the expectation.
 *
 * Reported from the trace rather than asserted against the expectation: this
 * page shows what happened, and a visitor comparing the two lines is doing the
 * more convincing version of what a green check would claim.
 */
function describeActual(refused: number, status: string): string {
  const refusals = refused > 0 ? `, ${refused} tool call refused in code` : "";
  return `run ${status}${refusals}`;
}
