"""Shared column mixins.

Money is stored as an integer count of minor units (paise for INR) — never a
float, never a Decimal-typed float round-trip (ADR 0001). Razorpay itself works
in minor units, and every downstream expected-recovery calculation is
deterministic code that must be exactly reproducible.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, declarative_mixin, mapped_column

#: ISO 4217 alphabetic code length.
CURRENCY_CODE_LENGTH = 3
DEFAULT_CURRENCY = "INR"


@declarative_mixin
class UUIDPrimaryKeyMixin:
    """UUID v4 primary key, generated application-side (ADR 0002)."""

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


@declarative_mixin
class TimestampMixin:
    """created_at / updated_at, both timezone-aware UTC."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


@declarative_mixin
class CreatedAtMixin:
    """created_at only — for append-only tables that must never be updated."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )


def money_column(*, nullable: bool = False) -> Mapped[int]:
    """A monetary amount in minor units (paise for INR).

    BigInteger, not Integer: INR paise overflow a 32-bit column at roughly
    21 crore rupees, which is not a safe ceiling for aggregate figures.

    Non-negativity is enforced per-table with an explicit CHECK constraint so
    the constraint carries a readable name in migrations.
    """
    return mapped_column(
        BigInteger,
        nullable=nullable,
    )


def currency_column(*, nullable: bool = False) -> Mapped[str]:
    return mapped_column(
        String(CURRENCY_CODE_LENGTH),
        nullable=nullable,
        default=DEFAULT_CURRENCY,
    )


def enum_check(column: str, values: tuple[str, ...], *, name: str) -> CheckConstraint:
    """CHECK constraint pinning a VARCHAR column to an enum vocabulary."""
    rendered = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({rendered})", name=name)
