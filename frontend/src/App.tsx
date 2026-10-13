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
import { fetchCatalog, streamLive, streamReplay, type Subscription } from "./stream";
import type { Catalog, LiveStatus, Notice, Scenario, TraceEvent } from "./wire";

type Status = "idle" | "playing" | "done" | "error";

/** What the visitor asked for, which is not always what they got. */
type Mode = "replay" | "live";

export default function App() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [status, setStatus] = useState<Status>("idle");
  const [streamError, setStreamError] = useState<string | null>(null);
  const [requested, setRequested] = useState<Mode>("replay");
  // What the server actually served. A live request that no control allowed
  // comes back as a recording, and the page has to say which one you watched
  // rather than keep the label the button implied.
  const [notice, setNotice] = useState<Notice | null>(null);
  const subscription = useRef<Subscription | null>(null);

  useEffect(() => {
    fetchCatalog()
      .then((loaded) => {
        setCatalog(loaded);
        setSelectedId(loaded.scenarios[0]?.id ?? null);
      })
      .catch((error: unknown) => setLoadError(String(error)));
  }, []);

  /**
   * Re-read the catalog after a live run.
   *
   * Two things are stale by then: the visitor's remaining live runs and the
   * day's token budget, and — because a live run creates a real refund — the
   * order's `prior_refunds`. A page still showing "no prior refunds" under an
   * order it just refunded is wrong about the thing it is demonstrating.
   */
  const refreshCatalog = useCallback(() => {
    fetchCatalog()
      .then(setCatalog)
      .catch(() => {
        /* The run is already on screen; a stale footer is not worth an error. */
      });
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
    setNotice(null);
  }, []);

  const start = useCallback(
    (mode: Mode) => {
      if (!selected) return;
      subscription.current?.close();
      setEvents([]);
      setStreamError(null);
      setNotice(null);
      setRequested(mode);
      setStatus("playing");

      const handlers = {
        onEvent: (event: TraceEvent) => setEvents((previous) => [...previous, event]),
        onDone: () => {
          setStatus("done");
          if (mode === "live") refreshCatalog();
        },
        onError: (message: string) => {
          setStreamError(message);
          setStatus("error");
        },
        onNotice: setNotice,
      };

      subscription.current =
        mode === "live" ? streamLive(selected.id, handlers) : streamReplay(selected.id, handlers);
    },
    [selected, refreshCatalog],
  );

  const play = useCallback(() => start("replay"), [start]);
  const runLive = useCallback(() => start("live"), [start]);

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
        onRunLive={runLive}
        live={catalog.live}
        simulatedToday={catalog.simulated_today}
      />

      {notice?.message && (
        <p className={notice.reason === "run_failed" ? "notice error" : "notice"} role="status">
          {notice.message}
        </p>
      )}
      {streamError && <p className="notice error">{streamError}</p>}

      <Trace events={events} status={status} />
      <SummaryBar
        events={events}
        scenario={selected}
        served={notice?.mode ?? requested}
        requested={requested}
      />
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
        <strong>What you are watching.</strong> By default, a recorded run of the real agent against{" "}
        <code>gpt-5</code>, replayed at the pace it happened — and where live mode is switched on,{" "}
        <em>Run live</em> calls the model now, inside a per-visitor rate limit and a daily token
        budget. The scenarios come from the same evaluation dataset the test suite grades against,
        so the demo and the measurements cannot disagree. Nothing here is scripted: the agent chose
        each tool from what the previous result told it.
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
  onRunLive,
  live,
  simulatedToday,
}: {
  scenario: Scenario;
  status: Status;
  onPlay: () => void;
  onRunLive: () => void;
  live: LiveStatus | undefined;
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
        <button
          type="button"
          className="secondary"
          onClick={onRunLive}
          disabled={status === "playing" || !live?.available}
          // Why the button is off, in the server's words. A disabled control
          // with no explanation reads as a broken page rather than a budget.
          title={live?.available ? "Run this scenario against the real model now" : liveWhy(live)}
        >
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
        Replay is a recorded run of the real agent, not a mock; Run live calls the model now. Either
        way the message is resolved on the server from the evaluation dataset — this page only ever
        sends a scenario id. Dates are relative to {simulatedToday}.
      </p>

      <LiveAllowance live={live} />
    </section>
  );
}

/** Why Run live is unavailable, for the button's tooltip. */
function liveWhy(live: LiveStatus | undefined): string {
  if (!live) return "Live mode is not available on this deployment.";
  return live.message ?? "Live mode is not available right now.";
}

/**
 * What a live run costs and what is left of it.
 *
 * On screen rather than only in the design docs, including the awkward part:
 * the budget is counted per server instance, so a deployment running several
 * has a real ceiling of this number times the instance count. Claiming a global
 * cap the code does not enforce would be the kind of overstatement this project
 * writes down instead of making (DD-015).
 */
function LiveAllowance({ live }: { live: LiveStatus | undefined }) {
  if (!live) return null;

  return (
    <p className="aside allowance">
      {live.available ? (
        <>
          Live runs: <strong>{live.runs_remaining_this_hour ?? live.runs_per_hour}</strong> left
          this hour of {live.runs_per_hour}.{" "}
        </>
      ) : (
        <>Live runs are unavailable right now. </>
      )}
      Daily budget {live.tokens_remaining_today.toLocaleString("en-US")} of{" "}
      {live.daily_token_budget.toLocaleString("en-US")} tokens remaining, counted{" "}
      {live.budget_scope} — so a deployment running more than one instance has a correspondingly
      higher real ceiling.
    </p>
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

function SummaryBar({
  events,
  scenario,
  served,
  requested,
}: {
  events: TraceEvent[];
  scenario: Scenario;
  /** What the server actually sent: a live run, or a recording. */
  served: Mode;
  /** What the visitor pressed. Different from `served` when a control fired. */
  requested: Mode;
}) {
  const summary = useMemo(() => toSummary(events), [events]);
  if (!summary) return null;

  return (
    <section className="summary">
      <div className="summary-line">
        <span className={served === "live" ? "mode mode-live" : "mode"}>
          {served === "live" ? "ran live just now" : "recorded run"}
          {requested === "live" && served === "replay" && " (live was unavailable)"}
        </span>
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
