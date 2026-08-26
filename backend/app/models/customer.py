"""Customer.

`lifetime_value` is money in minor units and feeds deterministic risk scoring
in Phase 3 — it must never become a float.

`contactable` and `contact_count` exist because the policy engine (Phase 7)
enforces customer opt-out and a maximum-contacts rule; those checks need
durable state, not a value recomputed per run.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, money_column

if TYPE_CHECKING:  # runtime resolution goes through SQLAlchemy's class registry
    from app.models.merchant import Merchant
    from app.models.order import Order


class Customer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "customers"

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    #: Minor units. Synthetic in demo data; labelled as such wherever reported.
    lifetime_value: Mapped[int] = money_column(nullable=False)

    #: Policy-engine inputs (Phase 7).
    contactable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    contact_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    merchant: Mapped["Merchant"] = relationship(back_populates="customers")
    orders: Mapped[list["Order"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "merchant_id", "external_customer_id", name="uq_customers_merchant_external"
        ),
        CheckConstraint("lifetime_value >= 0", name="lifetime_value_non_negative"),
        CheckConstraint("contact_count >= 0", name="contact_count_non_negative"),
    )
