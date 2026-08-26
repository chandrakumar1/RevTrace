"""Revenue risk — output of deterministic detection (Phase 3).

`amount_at_risk` and `confidence` are produced by the deterministic risk
engine, never by an LLM. `confidence` is stored as an integer in basis points
(0-10000) rather than a float, so that policy thresholds compare exactly and
reproducibly — a float threshold comparison is not a safe gate on money.

`is_true_positive` is ground-truth labelling for the Phase 11 evaluation
benchmark and is NULL for unlabelled cases.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import RiskStatus, RiskType
from app.models.mixins import (
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    currency_column,
    enum_check,
    money_column,
)

if TYPE_CHECKING:  # runtime resolution goes through SQLAlchemy's class registry
    from app.models.recovery_case import RecoveryCase


class RevenueRisk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "revenue_risks"

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    risk_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: Minor units. Deterministic engine output.
    amount_at_risk: Mapped[int] = money_column(nullable=False)
    currency: Mapped[str] = currency_column(nullable=False)

    #: Basis points, 0-10000. Integer so thresholds compare exactly.
    confidence_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Identifier of the deterministic rule that fired, for auditability.
    detection_rule: Mapped[str | None] = mapped_column(String(128), nullable=True)

    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RiskStatus.DETECTED.value, index=True
    )

    #: Phase 11 evaluation ground truth. NULL when unlabelled.
    is_true_positive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    recovery_cases: Mapped[list["RecoveryCase"]] = relationship(
        back_populates="risk", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("amount_at_risk >= 0", name="amount_at_risk_non_negative"),
        CheckConstraint(
            "confidence_bps >= 0 AND confidence_bps <= 10000",
            name="confidence_bps_in_range",
        ),
        CheckConstraint("char_length(currency) = 3", name="currency_iso4217"),
        enum_check("risk_type", RiskType.values(), name="risk_type_valid"),
        enum_check("status", RiskStatus.values(), name="status_valid"),
        Index("ix_revenue_risks_merchant_status_detected", "merchant_id", "status", "detected_at"),
    )
