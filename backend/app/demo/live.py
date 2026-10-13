"""The controls that make a public, key-holding endpoint safe to expose.

Replay costs nothing and cannot be abused, so everything in this module exists
for one button: **Run live**, which spends real tokens on a page anyone can
load. The design rule is that no single control is load-bearing — the flag, the
rate limit, the budget, the concurrency cap and the existing `max_steps` ceiling
each bound a different failure, and any one of them failing open still leaves
the others.

Two properties are worth stating plainly because they are the honest limits of
this implementation rather than oversights:

**The counters live in this process.** A platform that runs N instances has N
rate limiters and N budgets, so the real daily ceiling is the configured budget
times the instance count. The budget default is sized for that multiplier rather
than for a single process (DD-015), and Phase 2's shared state replaces this
with one counter when it brings the infrastructure that can hold it.

**Exhaustion is not an error.** Every control degrades to replay with a visible
notice instead of refusing. A visitor who clicks Run live on a spent budget
should see the agent work and read why this particular run was recorded — not a
stack trace, and not a dead button with no explanation.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

#: Why a request is being served from a recording instead of run live. Sent to
#: the page verbatim, so each one is phrased for a visitor rather than for a log.
DISABLED = "disabled"
NO_CREDENTIAL = "no_credential"
RATE_LIMITED = "rate_limited"
BUDGET_EXHAUSTED = "budget_exhausted"
BUSY = "busy"

LIVE = "live"
REPLAY = "replay"

#: Tokens held aside for one live run before it starts.
#:
#: Sized from the recordings rather than the average: a typical scenario spends
#: around 5,800 tokens, but S21 — the guardrail scenario, and the one a visitor
#: is most likely to click — spent 13,540. Reserving the average would let two
#: worst-case runs overshoot the day's cap between them, and since a
#: reservation is only held while a run is in flight, over-reserving costs
#: nothing except a slightly early switch to replay on the last run of the day.
RESERVE_PER_RUN = 14_000

#: What the page shows for each reason. The server owns this wording because the
#: server is what knows which control fired; a client guessing from a code would
#: drift the moment a reason is added.
NOTICES = {
    DISABLED: (
        "Live mode is switched off right now, so this is a recorded run of the "
        "same scenario against the real model."
    ),
    NO_CREDENTIAL: (
        "No model credential is configured on this deployment, so this is a "
        "recorded run of the same scenario against the real model."
    ),
    RATE_LIMITED: (
        "You have used your live runs for this hour. This is a recorded run of "
        "the same scenario against the real model."
    ),
    BUDGET_EXHAUSTED: (
        "The demo's daily token budget is spent, so this is a recorded run of "
        "the same scenario against the real model. Live runs resume tomorrow."
    ),
    BUSY: (
        "Another live run is in progress. This is a recorded run of the same "
        "scenario against the real model — try live again in a moment."
    ),
}

#: Injectable so the budget's daily reset and the rate limit's window are
#: testable without sleeping. Returns an aware UTC datetime: the budget rolls
#: over on the UTC date, which is the only definition that does not move with a
#: deployment's region.
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RateLimiter:
    """N events per key per window, as a sliding window of timestamps.

    A sliding window rather than a fixed bucket because a fixed bucket lets a
    visitor spend the whole allowance twice across a boundary. Keys are client
    IPs, so the memory is bounded by distinct visitors in one window and old
    entries are dropped on read rather than swept.
    """

    limit: int
    window_s: float = 3_600.0
    clock: Clock = utc_now
    _seen: dict[str, deque[datetime]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _prune(self, key: str, now: datetime) -> deque[datetime]:
        hits = self._seen.setdefault(key, deque())
        while hits and (now - hits[0]).total_seconds() >= self.window_s:
            hits.popleft()
        if not hits:
            # Dropped rather than left empty: a map that only ever grows is a
            # slow leak on a public endpoint.
            self._seen.pop(key, None)
            return deque()
        return hits

    def take(self, key: str) -> bool:
        """Consume one allowance for `key`. False when the window is full."""
        if self.limit <= 0:
            return False
        with self._lock:
            now = self.clock()
            hits = self._prune(key, now)
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            self._seen[key] = hits
            return True

    def refund(self, key: str) -> None:
        """Hand back the allowance just taken, for a run that never started.

        A later control refusing is not the visitor's doing, and charging them
        an hourly slot for it would mean a busy server quietly costs people
        their live runs. Drops the newest timestamp rather than the oldest, so
        the window still reflects the calls that did happen.
        """
        with self._lock:
            hits = self._seen.get(key)
            if hits:
                hits.pop()
            if hits is not None and not hits:
                self._seen.pop(key, None)

    def remaining(self, key: str) -> int:
        with self._lock:
            return max(self.limit - len(self._prune(key, self.clock())), 0)


@dataclass
class TokenBudget:
    """A daily token allowance, counted in this process and reset on UTC date.

    Reserved before a run and reconciled after it, because the real cost is only
    known once the provider reports usage. The reservation is what stops a burst
    of simultaneous runs from each seeing a healthy balance and collectively
    blowing through it — `max_steps` bounds a single run, so one scenario's worst
    case is a sound reservation.
    """

    daily_limit: int
    reserve_per_run: int
    clock: Clock = utc_now
    spent: int = 0
    _day: str = field(default="", repr=False)
    _reserved: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _roll(self) -> None:
        today = self.clock().date().isoformat()
        if today != self._day:
            self._day = today
            self.spent = 0
            # Reservations belong to runs that are already over by definition:
            # a run cannot outlive a date change under any configured ceiling.
            self._reserved = 0

    @property
    def committed(self) -> int:
        """Tokens spent plus tokens promised to runs still in flight."""
        return self.spent + self._reserved

    def reserve(self) -> bool:
        """Claim room for one run. False when the day's allowance is gone."""
        with self._lock:
            self._roll()
            if self.committed + self.reserve_per_run > self.daily_limit:
                return False
            self._reserved += self.reserve_per_run
            return True

    def settle(self, tokens: int) -> None:
        """Release a reservation and charge what the run actually cost.

        Charges the real usage even when it overran the reservation: a run that
        cost more than expected has still cost it, and hiding that would let the
        day's true spend exceed the cap silently.
        """
        with self._lock:
            self._roll()
            self._reserved = max(self._reserved - self.reserve_per_run, 0)
            self.spent += max(tokens, 0)

    def release(self) -> None:
        """Give back a reservation for a run that never happened."""
        with self._lock:
            self._reserved = max(self._reserved - self.reserve_per_run, 0)

    @property
    def remaining(self) -> int:
        with self._lock:
            self._roll()
            return max(self.daily_limit - self.committed, 0)


@dataclass
class Decision:
    """Which mode a request got, and the reason if it is not the one asked for."""

    mode: str
    reason: str | None = None

    @property
    def is_live(self) -> bool:
        return self.mode == LIVE

    def notice(self) -> dict[str, Any] | None:
        """The control frame the page renders, or nothing when live was granted."""
        if self.is_live or self.reason is None:
            return None
        return {
            "mode": self.mode,
            "reason": self.reason,
            "message": NOTICES.get(
                self.reason,
                "This is a recorded run of the same scenario against the real model.",
            ),
        }


@dataclass
class LiveGate:
    """Every control from the Phase 1.5 plan, in the order it is cheapest to fail.

    Order matters for cost, not correctness: the flag is a boolean, the rate
    limit is a dict lookup, and the budget reserves state that then has to be
    released if a later control refuses. Checking the cheap ones first means the
    common refusal does no work.
    """

    enabled: bool
    rate_limiter: RateLimiter
    budget: TokenBudget
    max_concurrent: int
    has_credential: Callable[[], bool]
    _in_flight: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def admit(self, visitor: str) -> Decision:
        """Decide whether this visitor gets a live run, and take the allowances.

        A `live` decision has already consumed a rate-limit slot, a budget
        reservation and a concurrency slot, so the caller **must** call
        `finish()` for it. Anything else consumed nothing.
        """
        if not self.enabled:
            return Decision(REPLAY, DISABLED)
        if not self.has_credential():
            return Decision(REPLAY, NO_CREDENTIAL)
        if not self.rate_limiter.take(visitor):
            return Decision(REPLAY, RATE_LIMITED)

        if not self._claim_slot():
            # Nothing was spent, so the visitor keeps the allowance they would
            # otherwise have burned on a run that never started.
            self.rate_limiter.refund(visitor)
            return Decision(REPLAY, BUSY)

        if not self.budget.reserve():
            self._release_slot()
            self.rate_limiter.refund(visitor)
            return Decision(REPLAY, BUDGET_EXHAUSTED)

        return Decision(LIVE)

    def finish(self, tokens: int) -> None:
        """Settle a live run: charge what it cost and free its slot."""
        self.budget.settle(tokens)
        self._release_slot()

    def abandon(self) -> None:
        """A live run that produced no usage at all — a provider error, say."""
        self.budget.release()
        self._release_slot()

    def status(self, visitor: str | None = None) -> dict[str, Any]:
        """What the page says about live mode before anyone clicks it.

        `available` is the only field the button depends on. The rest is shown
        so a visitor understands why the button is off, and so the per-instance
        caveat is visible rather than buried in a design document.
        """
        reason: str | None = None
        if not self.enabled:
            reason = DISABLED
        elif not self.has_credential():
            reason = NO_CREDENTIAL
        elif self.budget.remaining < self.budget.reserve_per_run:
            reason = BUDGET_EXHAUSTED
        elif visitor is not None and self.rate_limiter.remaining(visitor) <= 0:
            reason = RATE_LIMITED

        return {
            "available": reason is None,
            "reason": reason,
            "message": NOTICES.get(reason) if reason else None,
            "runs_per_hour": self.rate_limiter.limit,
            "runs_remaining_this_hour": (
                self.rate_limiter.remaining(visitor) if visitor is not None else None
            ),
            "daily_token_budget": self.budget.daily_limit,
            "tokens_remaining_today": self.budget.remaining,
            # Said on screen rather than only in the docs: the counter is
            # per-process, and claiming a global cap we do not enforce would be
            # the kind of overstatement this project writes down instead (DD-015).
            "budget_scope": "per server instance",
        }

    def _claim_slot(self) -> bool:
        with self._lock:
            if self._in_flight >= self.max_concurrent:
                return False
            self._in_flight += 1
            return True

    def _release_slot(self) -> None:
        with self._lock:
            self._in_flight = max(self._in_flight - 1, 0)


def build_gate(
    settings: Settings,
    *,
    has_credential: Callable[[], bool],
    clock: Clock = utc_now,
) -> LiveGate:
    """Assemble the gate from configuration.

    `has_credential` is a callable rather than a boolean so the gate reflects
    the credential at request time. A deployment can gain a key without a
    restart, and a gate that decided this once at import would keep sending
    everyone to replay afterwards.
    """
    return LiveGate(
        enabled=settings.demo_live_enabled,
        rate_limiter=RateLimiter(
            limit=settings.demo_live_runs_per_hour,
            window_s=3_600.0,
            clock=clock,
        ),
        budget=TokenBudget(
            daily_limit=settings.demo_live_daily_token_budget,
            reserve_per_run=RESERVE_PER_RUN,
            clock=clock,
        ),
        max_concurrent=settings.demo_live_max_concurrent,
        has_credential=has_credential,
    )
