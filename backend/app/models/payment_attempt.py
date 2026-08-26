"""Payment attempt — the atom of failure analysis.

`attempt_number` is what Scenario A ("customer attempts payment twice within a
defined window and fails") counts, so it is a stored column rather than a
window function computed at read time: detection must be reproducible and the
number must survive out-of-order event arrival.

`external_payment_id` is required to reconcile our record against the provider
during Phase 9 verification.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import PaymentMethod, PaymentStatus
from app.models.mixins import (
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    currency_column,
    enum_check,
    money_column,
)

if TYPE_CHECKING:  # runtime resolution goes through SQLAlchemy's class registry
    from app.models.order import Order


class PaymentAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payment_attempts"

    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    external_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    #: Minor units.
    amount: Mapped[int] = money_column(nullable=False)
    currency: Mapped[str] = currency_column(nullable=False)

    payment_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PaymentMethod.UNKNOWN.value
    )
    #: Payment provider name. Kept provider-neutral; "razorpay" from Phase 8.
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="razorpay")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: When the attempt actually occurred, as distinct from when we recorded it.
    attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    order: Mapped["Order"] = relationship(back_populates="payment_attempts")

    __table_args__ = (
        UniqueConstraint(
            "provider", "external_payment_id", name="uq_payment_attempts_provider_external"
        ),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint("char_length(currency) = 3", name="currency_iso4217"),
        enum_check("status", PaymentStatus.values(), name="status_valid"),
        enum_check("payment_method", PaymentMethod.values(), name="payment_method_valid"),
    )
