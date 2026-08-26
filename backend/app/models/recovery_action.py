"""Recovery action — a single bounded operation within a case.

`approved` and `executed` are separate booleans with a database-level guarantee
that executed implies approved. This is the finest-grained expression of the
architecture rule: nothing executes that was not approved, and the database
refuses to store the contradiction regardless of what any caller believes.

`idempotency_key` prevents a retried or duplicated execution from performing
the same money-moving operation twice.
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ActionType
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check

if TYPE_CHECKING:  # runtime resolution goes through SQLAlchemy's class registry
    from app.models.recovery_case import RecoveryCase


class RecoveryAction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "recovery_actions"

    case_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    action_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Who approved. Non-null whenever approved is true (Phase 7/9).
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    executed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Guards against duplicate money-moving execution on retry.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    case: Mapped["RecoveryCase"] = relationship(back_populates="actions")

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_recovery_actions_idempotency_key"),
        enum_check("action_type", ActionType.values(), name="action_type_valid"),
        # The architecture rule, enforced by the database.
        CheckConstraint("NOT executed OR approved", name="executed_requires_approved"),
        CheckConstraint(
            "NOT approved OR approved_at IS NOT NULL", name="approved_requires_timestamp"
        ),
        CheckConstraint(
            "NOT executed OR executed_at IS NOT NULL", name="executed_requires_timestamp"
        ),
    )
