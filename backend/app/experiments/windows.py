"""Observation windows: opening them, and sealing them shut.

A window is the fixed period after detection during which an outcome counts.
Two rules make it worth having:

**Nothing may be analysed before its window seals.** An open window is a number
still moving, and estimating from one is peeking — the failure mode a fixed
horizon exists to prevent. `sealed` is the gate, and `seal()` refuses to close a
window before its own close time even when asked directly.

**Nothing changes after it seals.** A sealed outcome is final. Events that
arrive afterwards are real and are recorded — as `decision_type='verify'` with
`action='LATE_EVENT'` — but they do not move the number. Silently absorbing a
late recovery would mean the measured effect depended on how long anyone waited
before looking.

Durations are per risk type, as pre-registered: 72h for repeated payment
failure, 24h for checkout abandonment, 168h for a subscription failure. A
`reconciliation_mismatch` gets no window at all — its amount at risk is zero by
definition (ADR 0007), so there is nothing to observe.

No clock, anywhere. `as_of` is injected exactly as it is in detection and
assignment, so a sweep is reproducible and a test can place itself either side
of a boundary precisely. There is no scheduler, no cron, and no background
worker: `seal_due()` is a plain callable, and whatever invokes it supplies the
instant.

This module opens and seals. It does not decide what the outcome *was* —
recording a recovery is verification's job, and keeping the two apart is what
stops the sealer quietly editing the number it is meant to freeze.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.experiments.assignment import EXCLUDED_RISK_TYPES
from app.models import CaseOutcome, RevenueRisk
from app.models.enums import DecisionType, RiskType

#: Pre-registered observation window per risk type, in whole hours.
#:
#: The three durations reflect how long each leak plausibly takes to resolve on
#: its own: a failed payment retried within days, an abandoned checkout within
#: one, a subscription across a full billing week.
WINDOW_HOURS: Mapping[str, int] = {
    RiskType.REPEATED_PAYMENT_FAILURE.value: 72,
    RiskType.CHECKOUT_ABANDONMENT.value: 24,
    RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value: 168,
}

#: Audit action for an event that arrived after its window sealed.
LATE_EVENT_ACTION = "LATE_EVENT"


class WindowError(ValueError):
    """A window could not be opened or sealed, and the caller should know why."""


def has_window(risk_type: str) -> bool:
    """Whether this risk type is observed at all.

    Excluded types are exactly the ones assignment excludes, imported rather
    than re-listed: a risk that is never randomised must never acquire a window,
    or the sweeper would seal outcomes for units no experiment contains.
    """
    return risk_type in WINDOW_HOURS and risk_type not in EXCLUDED_RISK_TYPES


def window_hours(risk_type: str) -> int:
    """The pre-registered duration for a risk type."""
    if risk_type not in RiskType.values():
        raise WindowError(f"unknown risk_type {risk_type!r}")
    if not has_window(risk_type):
        raise WindowError(
            f"{risk_type} is excluded from observation: it carries no amount at risk, "
            "so there is nothing to observe"
        )
    return WINDOW_HOURS[risk_type]


@dataclass(frozen=True, slots=True)
class Window:
    """One observation period. Pure — computing it touches no database."""

    risk_id: uuid.UUID
    risk_type: str
    opens_at: datetime
    closes_at: datetime

    @property
    def duration_hours(self) -> int:
        return int((self.closes_at - self.opens_at).total_seconds()) // 3600

    def is_closed_at(self, as_of: datetime) -> bool:
        """Closed once `as_of` reaches the close time. Inclusive at the boundary."""
        return as_of >= self.closes_at

    def contains(self, moment: datetime) -> bool:
        """Whether an event at `moment` falls inside the observed period."""
        return self.opens_at <= moment < self.closes_at


def window_for(risk_type: str, opened_at: datetime) -> tuple[datetime, datetime]:
    """The (opens_at, closes_at) pair for a risk type. Deterministic."""
    if opened_at.tzinfo is None:
        raise WindowError("opened_at must be timezone-aware")
    return opened_at, opened_at + timedelta(hours=window_hours(risk_type))


def plan_window(risk: RevenueRisk) -> Window | None:
    """The window a risk should get, or `None` if it is not observed.

    Opens at `detected_at` — the moment the unit entered the population — not at
    whenever the sweeper happens to run. A window that opened on sweep time
    would vary with operational timing rather than with the case.
    """
    if not has_window(risk.risk_type):
        return None

    opens_at, closes_at = window_for(risk.risk_type, risk.detected_at)
    return Window(
        risk_id=risk.id,
        risk_type=risk.risk_type,
        opens_at=opens_at,
        closes_at=closes_at,
    )


# -- persistence ----------------------------------------------------------


def existing_outcome(session: Session, risk_id: uuid.UUID) -> CaseOutcome | None:
    statement = select(CaseOutcome).where(CaseOutcome.risk_id == risk_id)
    return session.execute(statement).scalars().first()


def open_window(session: Session, risk: RevenueRisk) -> CaseOutcome | None:
    """Create the outcome row that holds a risk's window.

    Idempotent: a risk already carrying an outcome keeps the one it has, window
    bounds included. Re-opening would silently move a boundary that an analysis
    may already have relied on.

    Returns `None` for a risk type that is not observed.
    """
    window = plan_window(risk)
    if window is None:
        return None

    found = existing_outcome(session, risk.id)
    if found is not None:
        return found

    outcome = CaseOutcome(
        risk_id=window.risk_id,
        window_opens_at=window.opens_at,
        window_closes_at=window.closes_at,
        sealed=False,
        recovered=False,
        recovered_amount=0,
    )
    session.add(outcome)
    session.flush()
    return outcome


def is_sealable(outcome: CaseOutcome, as_of: datetime) -> bool:
    """Whether this outcome may be sealed at `as_of`."""
    return not outcome.sealed and as_of >= outcome.window_closes_at


def seal(session: Session, outcome: CaseOutcome, as_of: datetime) -> bool:
    """Seal one outcome. Returns True if this call sealed it.

    Refuses to seal early. Sealing a window before its close time would freeze a
    number that was still moving, which is the same error as peeking — just
    made permanent.

    Idempotent: an already-sealed outcome is returned untouched, keeping its
    original `sealed_at` rather than being re-stamped with a later one.
    """
    if as_of.tzinfo is None:
        raise WindowError("as_of must be timezone-aware")

    if outcome.sealed:
        return False

    if as_of < outcome.window_closes_at:
        raise WindowError(
            f"window for risk {outcome.risk_id} closes at {outcome.window_closes_at.isoformat()}; "
            f"refusing to seal at {as_of.isoformat()}. Sealing early freezes a number "
            "that is still moving."
        )

    outcome.sealed = True
    outcome.sealed_at = as_of
    session.flush()
    return True


@dataclass(frozen=True, slots=True)
class SealRunSummary:
    """What one sweep did."""

    as_of: datetime
    examined: int = 0
    sealed: int = 0
    still_open: int = 0
    already_sealed: int = 0


def due_for_sealing(session: Session, as_of: datetime) -> list[CaseOutcome]:
    """Unsealed outcomes whose window has closed, oldest first.

    Ordered so a sweep is reproducible and a partial run resumes predictably.
    """
    statement = (
        select(CaseOutcome)
        .where(
            CaseOutcome.sealed.is_(False),
            CaseOutcome.window_closes_at <= as_of,
        )
        .order_by(CaseOutcome.window_closes_at, CaseOutcome.risk_id)
    )
    return list(session.execute(statement).scalars())


def seal_due(session: Session, as_of: datetime) -> SealRunSummary:
    """Seal every window that has closed by `as_of`.

    The sweeper, as a plain function. No scheduler, no cron, no background
    worker: the caller supplies the instant, which is what makes a sweep
    reproducible and lets a test sit exactly on a boundary.
    """
    if as_of.tzinfo is None:
        raise WindowError("as_of must be timezone-aware")

    due = due_for_sealing(session, as_of)
    sealed_now = sum(1 for outcome in due if seal(session, outcome, as_of))

    still_open = session.execute(
        select(CaseOutcome).where(
            CaseOutcome.sealed.is_(False),
            CaseOutcome.window_closes_at > as_of,
        )
    ).scalars()

    already = session.execute(select(CaseOutcome).where(CaseOutcome.sealed.is_(True))).scalars()

    return SealRunSummary(
        as_of=as_of,
        examined=len(due),
        sealed=sealed_now,
        still_open=len(list(still_open)),
        already_sealed=len(list(already)) - sealed_now,
    )


# -- late events ----------------------------------------------------------


def is_late(outcome: CaseOutcome, occurred_at: datetime) -> bool:
    """Whether an event arrived too late to count.

    Late means *after the window closed*, not merely after it was sealed. The
    sweeper may run at any time, so keying on `sealed_at` would make lateness
    depend on operational timing rather than on the pre-registered window.
    """
    return occurred_at >= outcome.window_closes_at


def late_event_entry(
    outcome: CaseOutcome,
    occurred_at: datetime,
    *,
    external_event_id: str | None = None,
    event_type: str | None = None,
) -> dict[str, object]:
    """The audit payload for an event that arrived after its window.

    Uses the existing convention rather than a new vocabulary entry:
    `decision_type='verify'` with `action='LATE_EVENT'`. The event is recorded
    and excluded, never quietly absorbed — the evaluation report states the
    count, because a late recovery folded into the total would make the measured
    effect a function of when someone looked.
    """
    return {
        "risk_id": outcome.risk_id,
        "actor": "engine",
        "action": LATE_EVENT_ACTION,
        "decision_type": DecisionType.VERIFY.value,
        "is_execution": False,
        "reason": (
            "Event occurred after the observation window closed; recorded for the "
            "evaluation report and excluded from the estimate."
        ),
        "numeric_snapshot": {
            "occurred_at": occurred_at.isoformat(),
            "window_closes_at": outcome.window_closes_at.isoformat(),
            "sealed_at": outcome.sealed_at.isoformat() if outcome.sealed_at else None,
            "seconds_late": int((occurred_at - outcome.window_closes_at).total_seconds()),
            "external_event_id": external_event_id,
            "event_type": event_type,
        },
    }


def open_windows_for(
    session: Session,
    risks: Sequence[RevenueRisk],
) -> tuple[int, int]:
    """Open a window for each observed risk. Returns (opened, skipped)."""
    opened = skipped = 0
    for risk in risks:
        if open_window(session, risk) is None:
            skipped += 1
        else:
            opened += 1
    return opened, skipped


__all__ = [
    "LATE_EVENT_ACTION",
    "WINDOW_HOURS",
    "SealRunSummary",
    "Window",
    "WindowError",
    "due_for_sealing",
    "existing_outcome",
    "has_window",
    "is_late",
    "is_sealable",
    "late_event_entry",
    "open_window",
    "open_windows_for",
    "plan_window",
    "seal",
    "seal_due",
    "window_for",
    "window_hours",
]
