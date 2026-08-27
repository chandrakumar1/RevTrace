"""Detection run orchestration.

The one place where the pure engine meets the database:

    load events → reconstruct → reconcile against stored risks → persist

Everything between the load and the persist is pure. The engine never sees a
session, and this module contains no detection logic of its own — it moves data
and applies decisions the engine already made.

`as_of` is required. A detection run that read the clock would not be
reproducible, and reproducibility is the whole basis of the audit trail.

Detection writes to `revenue_risks` and nothing else. No `recovery_cases`, no
`recovery_actions`, no `audit_events` — identifying a risk is not authorising a
response to it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.engine.detectors.config import DEFAULT_CONFIG, DetectorConfig
from app.engine.resolution import (
    DetectionDelta,
    KnownRisk,
    RiskResolution,
    reconcile_order,
    reconcile_subscription,
)
from app.repositories import event_repository, risk_repository
from app.services.tracing.reconstruction import reconstruct_merchant


@dataclass(frozen=True, slots=True)
class DetectionRunSummary:
    """What one detection run did.

    `total_recovered` is reported here rather than stored: `revenue_risks` has
    no column for a recovered amount, and inventing one would mean writing
    recovery state that belongs to Phase 6.
    """

    merchant_id: uuid.UUID
    as_of: datetime
    orders_examined: int = 0
    subscriptions_examined: int = 0
    events_examined: int = 0
    risks_created: int = 0
    risks_unchanged: int = 0
    risks_resolved: int = 0
    #: Integer minor units, open risks only.
    total_amount_at_risk: int = 0
    #: Integer minor units collected by resolutions in this run.
    total_recovered: int = 0
    resolutions: tuple[RiskResolution, ...] = field(default_factory=tuple)

    @property
    def risks_touched(self) -> int:
        return self.risks_created + self.risks_unchanged + self.risks_resolved


def _known_for_order(known: tuple[KnownRisk, ...], order_id: uuid.UUID) -> tuple[KnownRisk, ...]:
    return tuple(risk for risk in known if risk.order_id == order_id)


def _known_without_order(known: tuple[KnownRisk, ...]) -> tuple[KnownRisk, ...]:
    """Subscription risks carry no order."""
    return tuple(risk for risk in known if risk.order_id is None)


def run_detection(
    session: Session,
    merchant_id: uuid.UUID,
    as_of: datetime,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> DetectionRunSummary:
    """Detect, reconcile, and persist for one merchant."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")

    events = event_repository.events_for_merchant(session, merchant_id)
    timeline = reconstruct_merchant(merchant_id, events)
    known = risk_repository.known_risks_for_merchant(session, merchant_id)

    deltas: list[DetectionDelta] = []

    for order in timeline.orders:
        deltas.append(
            reconcile_order(order, _known_for_order(known, order.order_id), as_of, config)
        )

    if timeline.subscriptions:
        subscription_known = _known_without_order(known)
        for subscription in timeline.subscriptions:
            deltas.append(reconcile_subscription(subscription, subscription_known, as_of, config))

    created = 0
    unchanged = 0
    resolved = 0
    recovered = 0
    resolutions: list[RiskResolution] = []

    for delta in deltas:
        for finding in delta.new_findings:
            risk_repository.insert_finding(session, finding)
            created += 1

        for finding in delta.unchanged_findings:
            risk_repository.refresh_finding(session, finding)
            unchanged += 1

        for resolution in delta.resolutions:
            if risk_repository.apply_resolution(session, resolution) is not None:
                resolved += 1
                recovered += resolution.amount_recovered
                resolutions.append(resolution)

    open_rows = risk_repository.risks_for_merchant(session, merchant_id, only_open=True)

    return DetectionRunSummary(
        merchant_id=merchant_id,
        as_of=as_of,
        orders_examined=len(timeline.orders),
        subscriptions_examined=len(timeline.subscriptions),
        events_examined=len(events),
        risks_created=created,
        risks_unchanged=unchanged,
        risks_resolved=resolved,
        total_amount_at_risk=sum(row.amount_at_risk for row in open_rows),
        total_recovered=recovered,
        resolutions=tuple(resolutions),
    )


__all__ = ["DetectionRunSummary", "run_detection"]
