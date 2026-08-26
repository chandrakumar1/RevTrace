"""Merchant — the tenant root for every other entity."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import (
    CURRENCY_CODE_LENGTH,
    DEFAULT_CURRENCY,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

if TYPE_CHECKING:  # runtime resolution goes through SQLAlchemy's class registry
    from app.models.customer import Customer
    from app.models.order import Order


class Merchant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "merchants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH), nullable=False, default=DEFAULT_CURRENCY
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")

    customers: Mapped[list["Customer"]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("char_length(currency) = 3", name="currency_iso4217"),
        CheckConstraint("char_length(name) > 0", name="name_not_blank"),
    )
