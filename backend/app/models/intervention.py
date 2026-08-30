"""Intervention catalogue — the bounded set of actions that may ever be taken.

A catalogue rather than free-form parameters, because the policy engine needs
per-action limits it can enforce deterministically: what one costs, how long
before it may be repeated, how many times a customer may receive it, and whether
it crosses the RBI additional-factor-authentication threshold for recurring
debits.

`unit_cost` is minor units, so an action's cost enters the net-value calculation
as an exact integer rather than a float.

Seeded, not user-created. Phase 8 ships exactly one action type — payment link —
and the catalogue exists so that adding a second is a data change with limits
attached rather than a code path with none.
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import InterventionChannel
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check, money_column


class Intervention(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "interventions"

    #: Stable identifier used by the policy engine and the audit trail.
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    #: Minor units. Enters net incremental value as an exact integer.
    unit_cost: Mapped[int] = money_column(nullable=False)

    cooldown_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)

    #: True when the action crosses the RBI additional-factor-authentication
    #: threshold for recurring debits and therefore needs explicit consent.
    requires_afa: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    max_per_customer_per_month: Mapped[int] = mapped_column(Integer, nullable=False, default=2)

    #: Lets an action be retired without deleting the history that references it.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    __table_args__ = (
        UniqueConstraint("code", name="uq_interventions_code"),
        enum_check("channel", InterventionChannel.values(), name="channel_valid"),
        CheckConstraint("char_length(code) > 0", name="code_not_blank"),
        CheckConstraint("unit_cost >= 0", name="unit_cost_non_negative"),
        CheckConstraint("cooldown_hours >= 0", name="cooldown_hours_non_negative"),
        # Zero would mean "never allowed", which is what is_active is for.
        CheckConstraint(
            "max_per_customer_per_month > 0", name="max_per_customer_per_month_positive"
        ),
    )
