"""Detection run request and response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import UtcDatetime
from app.services.detection.service import DetectionRunSummary


class DetectionRunRequest(BaseModel):
    """Ask for a detection run over one merchant.

    `as_of` is required and has no default. A run that read the server clock
    would not be reproducible, and reproducibility is what makes the result
    auditable.
    """

    model_config = ConfigDict(extra="forbid")

    merchant_id: uuid.UUID
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return value


class ResolutionOut(BaseModel):
    """One risk closed by this run."""

    risk_type: str
    order_id: uuid.UUID | None
    previous_status: str
    new_status: str
    reason: str
    #: Integer minor units. Zero unless money actually moved.
    amount_recovered_minor: int


class DetectionRunResponse(BaseModel):
    """What the run did.

    `total_recovered_minor` is reported, not stored: `revenue_risks` has no
    column for it, and recovery accounting belongs to Phase 6.
    """

    merchant_id: uuid.UUID
    as_of: UtcDatetime
    orders_examined: int
    subscriptions_examined: int
    events_examined: int
    risks_created: int
    risks_unchanged: int
    risks_resolved: int
    total_amount_at_risk_minor: int
    total_recovered_minor: int
    resolutions: list[ResolutionOut] = Field(default_factory=list)

    @classmethod
    def from_summary(cls, summary: DetectionRunSummary) -> DetectionRunResponse:
        return cls(
            merchant_id=summary.merchant_id,
            as_of=summary.as_of,
            orders_examined=summary.orders_examined,
            subscriptions_examined=summary.subscriptions_examined,
            events_examined=summary.events_examined,
            risks_created=summary.risks_created,
            risks_unchanged=summary.risks_unchanged,
            risks_resolved=summary.risks_resolved,
            total_amount_at_risk_minor=summary.total_amount_at_risk,
            total_recovered_minor=summary.total_recovered,
            resolutions=[
                ResolutionOut(
                    risk_type=resolution.natural_key[2],
                    order_id=resolution.natural_key[1],
                    previous_status=resolution.previous_status,
                    new_status=resolution.new_status,
                    reason=resolution.reason,
                    amount_recovered_minor=resolution.amount_recovered,
                )
                for resolution in summary.resolutions
            ],
        )
