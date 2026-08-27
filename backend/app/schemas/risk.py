"""Revenue-risk response schemas.

Money crosses the wire as an integer count of minor units under `*_minor` keys.
Formatting for display is the consumer's job.

`confidence_bps` always travels with `confidence_is_synthetic_heuristic`. It is
a reproducible measure of evidence strength, never a calibrated probability, and
the flag exists so a consumer cannot render it as one by accident.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.models import RevenueRisk
from app.schemas.common import UtcDatetime


class RiskSummary(BaseModel):
    """One risk, as it appears in a list."""

    risk_id: uuid.UUID
    merchant_id: uuid.UUID
    order_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    order_ref: str | None = None
    risk_type: str
    status: str
    amount_at_risk_minor: int
    currency: str
    confidence_bps: int
    #: Always true in Phase 3. Never render this score as a probability.
    confidence_is_synthetic_heuristic: bool = True
    detection_rule: str | None
    detected_at: UtcDatetime

    @classmethod
    def from_row(cls, row: RevenueRisk, order_ref: str | None = None) -> RiskSummary:
        return cls(
            risk_id=row.id,
            merchant_id=row.merchant_id,
            order_id=row.order_id,
            customer_id=row.customer_id,
            order_ref=order_ref,
            risk_type=row.risk_type,
            status=row.status,
            amount_at_risk_minor=row.amount_at_risk,
            currency=row.currency,
            confidence_bps=row.confidence_bps,
            detection_rule=row.detection_rule,
            detected_at=row.detected_at,
        )


class RiskListResponse(BaseModel):
    items: list[RiskSummary] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class RiskDetail(RiskSummary):
    """One risk, with its lifecycle timestamps.

    Evidence is deliberately not inlined — it is derived from immutable events
    at read time and served by its own endpoint, so a detail fetch stays cheap.
    """

    created_at: UtcDatetime
    updated_at: UtcDatetime
    #: True for Phase 11 evaluation labelling; NULL until then.
    is_true_positive: bool | None = None
    evidence_url: str


class AttemptOut(BaseModel):
    attempt_number: int
    payment_ref: str
    outcome: str
    payment_method: str | None
    amount_minor: int
    currency: str | None
    failure_code: str | None
    failure_reason: str | None
    first_seen_at: UtcDatetime
    #: True when this attempt's own event never arrived and it was inferred.
    inferred: bool


class IntegrityOut(BaseModel):
    """Delivery-quality facts about the evidence.

    `duplicate_deliveries` reads 0 for anything rebuilt from stored rows: the
    unique constraint rejected the redeliveries at ingestion, so they never
    became rows. That is a property of the source, not a defect.
    """

    duplicate_deliveries: int
    out_of_order_deliveries: int
    max_delivery_lag_seconds: int
    inferred_gaps: int


class MoneyOut(BaseModel):
    """All figures in integer minor units."""

    order_amount_minor: int
    captured_minor: int
    failed_minor: int
    refunded_minor: int
    recovered_minor: int
    outstanding_minor: int


class RiskEvidence(BaseModel):
    """Evidence derived from immutable events at read time.

    Nothing here is stored on the risk row (ADR 0008): `events` is append-only,
    so evidence re-derived from the same rule and the same order is stable.
    """

    risk_id: uuid.UUID
    risk_type: str
    status: str
    detection_rule: str | None
    order_id: uuid.UUID | None
    order_ref: str | None
    order_state: str | None
    #: Present while the rule still fires; null once the risk has been resolved.
    current_reason: str | None
    contributing_event_ids: list[str] = Field(default_factory=list)
    attempts: list[AttemptOut] = Field(default_factory=list)
    integrity: IntegrityOut | None = None
    money: MoneyOut | None = None
    events_examined: int = 0
