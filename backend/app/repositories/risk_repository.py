"""Revenue-risk persistence.

`revenue_risks` is the **only** table detection writes to. Nothing here can
reach `recovery_cases`, `recovery_actions`, or `audit_events` — those are Phase
6-9 territory, and detection has no authority to create an approval, an action,
or an audit entry.

Identity is the application-level natural key `(merchant_id, order_id,
risk_type)`. There is no unique index behind it yet (approved decision 7), so
two concurrent detection runs for the same merchant could both miss an existing
row and insert duplicates. Detection runs are single-process today, which makes
that acceptable for Phase 3; a partial unique index is the obvious hardening
when concurrency arrives.

`order_id` is nullable — subscription risks carry none — so lookups must use
`IS NULL` rather than `= NULL`, which never matches anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.engine.detectors.base import RiskFinding
from app.engine.resolution import KnownRisk, RiskResolution
from app.models import RevenueRisk
from app.models.enums import RiskStatus


def _natural_key_filter(
    statement: Select[tuple[RevenueRisk]],
    merchant_id: uuid.UUID,
    order_id: uuid.UUID | None,
    risk_type: str,
) -> Select[tuple[RevenueRisk]]:
    filtered = statement.where(
        RevenueRisk.merchant_id == merchant_id,
        RevenueRisk.risk_type == risk_type,
    )
    # `= NULL` matches nothing in SQL; subscription risks have no order.
    if order_id is None:
        return filtered.where(RevenueRisk.order_id.is_(None))
    return filtered.where(RevenueRisk.order_id == order_id)


def find_by_natural_key(
    session: Session,
    merchant_id: uuid.UUID,
    order_id: uuid.UUID | None,
    risk_type: str,
) -> RevenueRisk | None:
    statement = _natural_key_filter(select(RevenueRisk), merchant_id, order_id, risk_type)
    return session.execute(statement).scalars().first()


def risks_for_merchant(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    only_open: bool = False,
) -> list[RevenueRisk]:
    """All risks for a merchant, in a stable order."""
    from app.engine.resolution import OPEN_STATUSES

    statement = select(RevenueRisk).where(RevenueRisk.merchant_id == merchant_id)
    if only_open:
        statement = statement.where(RevenueRisk.status.in_(sorted(OPEN_STATUSES)))

    statement = statement.order_by(RevenueRisk.detected_at, RevenueRisk.risk_type)
    return list(session.execute(statement).scalars())


def known_risks_for_merchant(session: Session, merchant_id: uuid.UUID) -> tuple[KnownRisk, ...]:
    """Hydrate stored rows into the value objects resolution works with."""
    return tuple(
        KnownRisk(
            merchant_id=row.merchant_id,
            order_id=row.order_id,
            risk_type=row.risk_type,
            status=row.status,
            amount_at_risk=row.amount_at_risk,
            detected_at=row.detected_at,
            detection_rule=row.detection_rule,
        )
        for row in risks_for_merchant(session, merchant_id)
    )


def insert_finding(session: Session, finding: RiskFinding) -> RevenueRisk:
    """Persist a newly detected risk.

    `is_true_positive` is deliberately left NULL: it is Phase 11 evaluation
    labelling, and detection must not label its own output.
    """
    risk = RevenueRisk(
        merchant_id=finding.merchant_id,
        customer_id=finding.customer_id,
        order_id=finding.order_id,
        risk_type=finding.risk_type,
        amount_at_risk=finding.amount_at_risk,
        currency=finding.currency or "INR",
        confidence_bps=finding.confidence_bps,
        detection_rule=finding.detection_rule,
        detected_at=finding.detected_at,
        status=RiskStatus.DETECTED.value,
    )
    session.add(risk)
    session.flush()
    return risk


def refresh_finding(session: Session, finding: RiskFinding) -> RevenueRisk | None:
    """Update a still-firing risk with the latest amount and confidence.

    `detected_at` is never overwritten — it records when the risk was *first*
    seen, and moving it would erase how long the risk has been open and break
    expiry.
    """
    existing = find_by_natural_key(
        session, finding.merchant_id, finding.order_id, finding.risk_type
    )
    if existing is None:
        return None

    existing.amount_at_risk = finding.amount_at_risk
    existing.confidence_bps = finding.confidence_bps
    existing.detection_rule = finding.detection_rule
    if finding.currency:
        existing.currency = finding.currency

    session.flush()
    return existing


def upsert_finding(session: Session, finding: RiskFinding) -> RevenueRisk:
    """Insert, or refresh if the natural key already exists."""
    refreshed = refresh_finding(session, finding)
    if refreshed is not None:
        return refreshed
    return insert_finding(session, finding)


def apply_resolution(session: Session, resolution: RiskResolution) -> RevenueRisk | None:
    """Move a stored risk to its resolved status.

    Only the status changes. `revenue_risks` has no column for a resolution
    reason or a recovered amount — those are reported in the run summary and,
    from Phase 6, belong to `recovery_cases`. Detection inventing them here
    would mean writing recovery state it has no authority over.
    """
    merchant_id, order_id, risk_type = resolution.natural_key
    existing = find_by_natural_key(session, merchant_id, order_id, risk_type)
    if existing is None:
        return None

    existing.status = resolution.new_status
    session.flush()
    return existing


def count_for_merchant(session: Session, merchant_id: uuid.UUID) -> int:
    return len(risks_for_merchant(session, merchant_id))


def open_risk_keys(session: Session, merchant_id: uuid.UUID) -> set[tuple[object, object, str]]:
    return {
        (row.merchant_id, row.order_id, row.risk_type)
        for row in risks_for_merchant(session, merchant_id, only_open=True)
    }


def bulk_insert_findings(session: Session, findings: Sequence[RiskFinding]) -> list[RevenueRisk]:
    return [insert_finding(session, finding) for finding in findings]
