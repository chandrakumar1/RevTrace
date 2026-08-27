"""Risk resolution — what happens to an open risk when the timeline moves on.

Detection alone is not enough. A detector that fires and never retracts leaves a
permanently wrong open risk the moment a late `payment.captured` or a successful
recovery arrives. Scenario D4 in the Phase 2 catalogue exists precisely to catch
that, and this module is the answer to it.

Pure and deterministic: no database, no network, no clock. `as_of` is supplied.
Nothing here writes anything — it produces *decisions*, and M7 applies them.

The comparison is always: **what does the current timeline say, versus what we
recorded last time?**

    still detected            → unchanged
    no longer detected, paid  → recovered
    no longer detected, not paid → false positive (evidence retracted)
    still detected, past expiry  → expired

Resolution never authorises anything. `recovered` is a statement that money
arrived, verified from the timeline — never an assertion that a recovery action
was approved or executed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.engine.detectors import detect_for_order, detect_for_subscription
from app.engine.detectors.base import RiskFinding, seconds_between
from app.engine.detectors.config import DEFAULT_CONFIG, DetectorConfig
from app.engine.risk_engine import captured_amount, recovered_amount
from app.models.enums import RiskStatus, RiskType
from app.services.tracing.state import OrderTimeline, SubscriptionTimeline

#: The upsert key shared by findings and stored risks.
NaturalKey = tuple[uuid.UUID, uuid.UUID | None, str]

#: Statuses that are still live and therefore resolvable.
OPEN_STATUSES: frozenset[str] = frozenset(
    {
        RiskStatus.DETECTED.value,
        RiskStatus.UNDER_INVESTIGATION.value,
        RiskStatus.RECOVERY_IN_PROGRESS.value,
    }
)

#: Statuses that are final. A resolved risk is never silently reopened.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        RiskStatus.RECOVERED.value,
        RiskStatus.UNRECOVERABLE.value,
        RiskStatus.FALSE_POSITIVE.value,
        RiskStatus.EXPIRED.value,
    }
)


@dataclass(frozen=True, slots=True)
class KnownRisk:
    """A risk recorded by an earlier detection run.

    In M7 this is hydrated from `revenue_risks`. Here it is a plain value so
    resolution stays testable without a database.
    """

    merchant_id: uuid.UUID
    order_id: uuid.UUID | None
    risk_type: str
    status: str
    amount_at_risk: int
    detected_at: datetime
    detection_rule: str | None = None

    def __post_init__(self) -> None:
        if self.risk_type not in RiskType.values():
            raise ValueError(f"invalid risk_type {self.risk_type!r}")
        if self.status not in RiskStatus.values():
            raise ValueError(f"invalid status {self.status!r}")

    @property
    def natural_key(self) -> NaturalKey:
        return (self.merchant_id, self.order_id, self.risk_type)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


@dataclass(frozen=True, slots=True)
class RiskResolution:
    """A decision about one previously-recorded risk."""

    natural_key: NaturalKey
    previous_status: str
    new_status: str
    reason: str
    resolved_at: datetime
    #: Integer minor units actually collected. Zero unless money moved.
    amount_recovered: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.amount_recovered, bool) or not isinstance(self.amount_recovered, int):
            raise TypeError("amount_recovered must be an integer count of minor units")
        if self.amount_recovered < 0:
            raise ValueError("amount_recovered must be non-negative")
        if self.new_status not in RiskStatus.values():
            raise ValueError(f"invalid new_status {self.new_status!r}")


@dataclass(frozen=True, slots=True)
class DetectionDelta:
    """The full outcome of a detection run against prior state.

    This is exactly what M7 persists: insert the new, leave the unchanged, apply
    the resolutions.
    """

    new_findings: tuple[RiskFinding, ...] = ()
    unchanged_keys: tuple[NaturalKey, ...] = ()
    resolutions: tuple[RiskResolution, ...] = ()
    #: Findings for risks that already existed and still fire. Carried so the
    #: persistence layer can refresh a stored amount or confidence without
    #: re-running detection.
    unchanged_findings: tuple[RiskFinding, ...] = ()

    @property
    def total_recovered(self) -> int:
        """Integer minor units recovered across all resolutions."""
        return sum(r.amount_recovered for r in self.resolutions)


def _resolution_for_closed_risk(
    known: KnownRisk,
    timeline: OrderTimeline,
    as_of: datetime,
) -> RiskResolution:
    """Decide why a risk that no longer fires has stopped firing."""
    if timeline.has_recovery_succeeded and timeline.reached_terminal_success:
        return RiskResolution(
            natural_key=known.natural_key,
            previous_status=known.status,
            new_status=RiskStatus.RECOVERED.value,
            reason=(
                "A recovery action was executed and payment was subsequently "
                "captured; the revenue was recovered."
            ),
            resolved_at=as_of,
            amount_recovered=recovered_amount(timeline),
        )

    if known.risk_type == RiskType.RECONCILIATION_MISMATCH.value and timeline.has_order_paid:
        return RiskResolution(
            natural_key=known.natural_key,
            previous_status=known.status,
            new_status=RiskStatus.RECOVERED.value,
            reason=(
                "order.paid arrived after the grace period had elapsed; the order "
                "has now reconciled. The event was late, not missing."
            ),
            resolved_at=as_of,
            # Nothing was recovered: the money had already arrived.
            amount_recovered=0,
        )

    if timeline.reached_terminal_success:
        return RiskResolution(
            natural_key=known.natural_key,
            previous_status=known.status,
            new_status=RiskStatus.RECOVERED.value,
            reason=(
                "Payment was captured after the risk was raised; the revenue was "
                "collected without intervention."
            ),
            resolved_at=as_of,
            amount_recovered=captured_amount(timeline),
        )

    return RiskResolution(
        natural_key=known.natural_key,
        previous_status=known.status,
        new_status=RiskStatus.FALSE_POSITIVE.value,
        reason=(
            "The detector no longer fires on this timeline and no payment was "
            "collected; the supporting evidence did not hold."
        ),
        resolved_at=as_of,
        amount_recovered=0,
    )


def _expired(known: KnownRisk, as_of: datetime, config: DetectorConfig) -> RiskResolution | None:
    age = seconds_between(known.detected_at, as_of)
    if age < config.risk_expiry_seconds:
        return None

    return RiskResolution(
        natural_key=known.natural_key,
        previous_status=known.status,
        new_status=RiskStatus.EXPIRED.value,
        reason=(
            f"Risk remained unresolved for {age} seconds and has passed the "
            "expiry window. Nothing was collected."
        ),
        resolved_at=as_of,
        amount_recovered=0,
    )


def reconcile_order(
    timeline: OrderTimeline,
    known_risks: Sequence[KnownRisk],
    as_of: datetime,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> DetectionDelta:
    """Run detection and reconcile the results against what was recorded before."""
    findings = detect_for_order(timeline, as_of, config)
    return _reconcile(findings, known_risks, timeline, as_of, config)


def reconcile_subscription(
    subscription: SubscriptionTimeline,
    known_risks: Sequence[KnownRisk],
    as_of: datetime,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> DetectionDelta:
    """Reconcile subscription risks.

    Subscription timelines carry no order, so a closed risk is resolved without
    consulting order-level capture state.
    """
    findings = detect_for_subscription(subscription, as_of, config)
    current = {finding.natural_key for finding in findings}

    new_findings: list[RiskFinding] = []
    unchanged: list[NaturalKey] = []
    resolutions: list[RiskResolution] = []
    seen = {risk.natural_key for risk in known_risks}

    for finding in findings:
        if finding.natural_key not in seen:
            new_findings.append(finding)

    by_key = {finding.natural_key: finding for finding in findings}
    unchanged_findings: list[RiskFinding] = []

    for known in known_risks:
        if not known.is_open:
            continue

        if known.natural_key in current:
            expiry = _expired(known, as_of, config)
            if expiry is not None:
                resolutions.append(expiry)
            else:
                unchanged.append(known.natural_key)
                unchanged_findings.append(by_key[known.natural_key])
            continue

        resolutions.append(
            RiskResolution(
                natural_key=known.natural_key,
                previous_status=known.status,
                new_status=RiskStatus.RECOVERED.value,
                reason=("A subsequent billing cycle succeeded; recurring revenue has resumed."),
                resolved_at=as_of,
                amount_recovered=0,
            )
        )

    return DetectionDelta(
        new_findings=tuple(new_findings),
        unchanged_keys=tuple(unchanged),
        resolutions=tuple(resolutions),
        unchanged_findings=tuple(unchanged_findings),
    )


def _reconcile(
    findings: Sequence[RiskFinding],
    known_risks: Sequence[KnownRisk],
    timeline: OrderTimeline,
    as_of: datetime,
    config: DetectorConfig,
) -> DetectionDelta:
    current = {finding.natural_key for finding in findings}
    seen = {risk.natural_key for risk in known_risks}

    new_findings = tuple(f for f in findings if f.natural_key not in seen)

    by_key = {finding.natural_key: finding for finding in findings}

    unchanged: list[NaturalKey] = []
    unchanged_findings: list[RiskFinding] = []
    resolutions: list[RiskResolution] = []

    for known in known_risks:
        # A terminal risk stays terminal. Re-running detection must not quietly
        # reopen something that was already closed.
        if not known.is_open:
            continue

        if known.natural_key in current:
            expiry = _expired(known, as_of, config)
            if expiry is not None:
                resolutions.append(expiry)
            else:
                unchanged.append(known.natural_key)
                unchanged_findings.append(by_key[known.natural_key])
            continue

        resolutions.append(_resolution_for_closed_risk(known, timeline, as_of))

    return DetectionDelta(
        new_findings=new_findings,
        unchanged_keys=tuple(unchanged),
        resolutions=tuple(resolutions),
        unchanged_findings=tuple(unchanged_findings),
    )


def known_from_findings(
    findings: Sequence[RiskFinding],
    status: str = RiskStatus.DETECTED.value,
) -> tuple[KnownRisk, ...]:
    """Turn a previous run's findings into the prior state for the next run.

    Used by tests and by any caller replaying detection without a database.
    """
    return tuple(
        KnownRisk(
            merchant_id=finding.merchant_id,
            order_id=finding.order_id,
            risk_type=finding.risk_type,
            status=status,
            amount_at_risk=finding.amount_at_risk,
            detected_at=finding.detected_at,
            detection_rule=finding.detection_rule,
        )
        for finding in findings
    )


__all__ = [
    "OPEN_STATUSES",
    "TERMINAL_STATUSES",
    "DetectionDelta",
    "KnownRisk",
    "NaturalKey",
    "RiskResolution",
    "known_from_findings",
    "reconcile_order",
    "reconcile_subscription",
]
