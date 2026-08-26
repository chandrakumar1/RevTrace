"""Order — the revenue opportunity that may or may not be realised."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import OrderStatus
from app.models.mixins import (
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    currency_column,
    enum_check,
    money_column,
)

if TYPE_CHECKING:  # runtime resolution goes through SQLAlchemy's class registry
    from app.models.customer import Customer
    from app.models.merchant import Merchant
    from app.models.payment_attempt import PaymentAttempt


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "orders"

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
    external_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Minor units (paise for INR).
    amount: Mapped[int] = money_column(nullable=False)
    currency: Mapped[str] = currency_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OrderStatus.CREATED.value, index=True
    )

    merchant: Mapped["Merchant"] = relationship(back_populates="orders")
    customer: Mapped["Customer | None"] = relationship(back_populates="orders")
    payment_attempts: Mapped[list["PaymentAttempt"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("merchant_id", "external_order_id", name="uq_orders_merchant_external"),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        CheckConstraint("char_length(currency) = 3", name="currency_iso4217"),
        enum_check("status", OrderStatus.values(), name="status_valid"),
    )
