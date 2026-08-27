"""Revenue-risk read endpoints.

Evidence is derived from immutable events at read time rather than stored
(ADR 0008), so this module reconstructs a timeline on demand.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import DbSession
from app.engine.detectors import detect_for_order, detect_for_subscription
from app.engine.risk_engine import money_breakdown
from app.models import Order, RevenueRisk
from app.models.enums import RiskStatus, RiskType
from app.repositories import event_repository
from app.schemas.risk import (
    AttemptOut,
    IntegrityOut,
    MoneyOut,
    RiskDetail,
    RiskEvidence,
    RiskListResponse,
    RiskSummary,
)
from app.services.tracing.reconstruction import reconstruct_merchant, reconstruct_order
from app.services.tracing.state import OrderTimeline

router = APIRouter(prefix="/risks", tags=["risks"])


def _order_refs(session: Session, risks: list[RevenueRisk]) -> dict[uuid.UUID, str | None]:
    """External order references, fetched in one query."""
    order_ids = [risk.order_id for risk in risks if risk.order_id is not None]
    if not order_ids:
        return {}

    rows = session.execute(
        select(Order.id, Order.external_order_id).where(Order.id.in_(order_ids))
    ).all()
    return {row[0]: row[1] for row in rows}


def _ref_for(refs: dict[uuid.UUID, str | None], order_id: uuid.UUID | None) -> str | None:
    """Look up an order reference. Subscription risks carry no order."""
    return refs.get(order_id) if order_id is not None else None


def _load_risk(session: Session, risk_id: uuid.UUID) -> RevenueRisk:
    risk = session.get(RevenueRisk, risk_id)
    if risk is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"risk {risk_id} not found"
        )
    return risk


def _attempts_out(timeline: OrderTimeline) -> list[AttemptOut]:
    return [
        AttemptOut(
            attempt_number=attempt.attempt_number,
            payment_ref=attempt.payment_ref,
            outcome=attempt.outcome.value,
            payment_method=attempt.payment_method,
            amount_minor=attempt.amount_minor,
            currency=attempt.currency,
            failure_code=attempt.failure_code,
            failure_reason=attempt.failure_reason,
            first_seen_at=attempt.first_seen_at,
            inferred=attempt.inferred,
        )
        for attempt in timeline.attempts
    ]


def _integrity_out(timeline: OrderTimeline) -> IntegrityOut:
    return IntegrityOut(
        duplicate_deliveries=timeline.integrity.duplicate_deliveries,
        out_of_order_deliveries=timeline.integrity.out_of_order_deliveries,
        max_delivery_lag_seconds=timeline.integrity.max_delivery_lag_seconds,
        inferred_gaps=timeline.integrity.inferred_gaps,
    )


def _money_out(timeline: OrderTimeline) -> MoneyOut:
    breakdown = money_breakdown(timeline)
    return MoneyOut(
        order_amount_minor=breakdown.order_amount,
        captured_minor=breakdown.captured,
        failed_minor=breakdown.failed,
        refunded_minor=breakdown.refunded,
        recovered_minor=breakdown.recovered,
        outstanding_minor=breakdown.outstanding,
    )


@router.get(
    "",
    response_model=RiskListResponse,
    summary="List detected revenue risks",
)
def list_risks(
    session: DbSession,
    merchant_id: Annotated[uuid.UUID | None, Query()] = None,
    risk_type: Annotated[str | None, Query()] = None,
    risk_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RiskListResponse:
    if risk_type is not None and risk_type not in RiskType.values():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown risk_type {risk_type!r}",
        )
    if risk_status is not None and risk_status not in RiskStatus.values():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown status {risk_status!r}",
        )

    statement = select(RevenueRisk)
    if merchant_id is not None:
        statement = statement.where(RevenueRisk.merchant_id == merchant_id)
    if risk_type is not None:
        statement = statement.where(RevenueRisk.risk_type == risk_type)
    if risk_status is not None:
        statement = statement.where(RevenueRisk.status == risk_status)

    all_rows = list(
        session.execute(
            statement.order_by(RevenueRisk.detected_at.desc(), RevenueRisk.risk_type)
        ).scalars()
    )
    page = all_rows[offset : offset + limit]
    refs = _order_refs(session, page)

    return RiskListResponse(
        items=[RiskSummary.from_row(row, _ref_for(refs, row.order_id)) for row in page],
        total=len(all_rows),
        limit=limit,
        offset=offset,
    )


@router.get("/{risk_id}", response_model=RiskDetail, summary="One detected risk")
def get_risk(risk_id: uuid.UUID, session: DbSession) -> RiskDetail:
    risk = _load_risk(session, risk_id)
    refs = _order_refs(session, [risk])

    order_ref = _ref_for(refs, risk.order_id)
    summary = RiskSummary.from_row(risk, order_ref)
    return RiskDetail(
        **summary.model_dump(),
        created_at=risk.created_at,
        updated_at=risk.updated_at,
        is_true_positive=risk.is_true_positive,
        evidence_url=f"/api/v1/risks/{risk.id}/evidence",
    )


@router.get(
    "/{risk_id}/evidence",
    response_model=RiskEvidence,
    summary="Evidence supporting a risk, derived from immutable events",
)
def get_risk_evidence(risk_id: uuid.UUID, session: DbSession) -> RiskEvidence:
    risk = _load_risk(session, risk_id)
    refs = _order_refs(session, [risk])

    # Subscription risks carry no order; their evidence comes from the
    # merchant's subscription timeline instead.
    if risk.order_id is None:
        events = event_repository.events_for_merchant(session, risk.merchant_id)
        merchant_timeline = reconstruct_merchant(risk.merchant_id, events)
        subscription = next(iter(merchant_timeline.subscriptions), None)

        reason = None
        contributing: list[str] = []
        if subscription is not None:
            findings = detect_for_subscription(subscription, risk.detected_at)
            if findings:
                reason = findings[0].reason
                contributing = list(findings[0].evidence_event_ids)

        return RiskEvidence(
            risk_id=risk.id,
            risk_type=risk.risk_type,
            status=risk.status,
            detection_rule=risk.detection_rule,
            order_id=None,
            order_ref=subscription.subscription_ref if subscription else None,
            order_state=None,
            current_reason=reason,
            contributing_event_ids=contributing,
            events_examined=len(subscription.events) if subscription else 0,
        )

    events = event_repository.events_for_order(session, risk.order_id)
    if not events:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no events found for order {risk.order_id}",
        )

    timeline = reconstruct_order(risk.order_id, events)

    # The reason is re-derived, not stored. A resolved risk no longer fires, so
    # there is legitimately no current reason to report.
    findings = detect_for_order(timeline, risk.detected_at)
    matching = next((f for f in findings if f.risk_type == risk.risk_type), None)

    return RiskEvidence(
        risk_id=risk.id,
        risk_type=risk.risk_type,
        status=risk.status,
        detection_rule=risk.detection_rule,
        order_id=risk.order_id,
        order_ref=_ref_for(refs, risk.order_id) or timeline.order_ref,
        order_state=timeline.state,
        current_reason=matching.reason if matching else None,
        contributing_event_ids=list(matching.evidence_event_ids) if matching else [],
        attempts=_attempts_out(timeline),
        integrity=_integrity_out(timeline),
        money=_money_out(timeline),
        events_examined=len(timeline.events),
    )
