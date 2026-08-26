"""Audit event — append-only record of every decision and action.

No ``updated_at``. There is deliberately no update path: an audit trail that can
be edited is not an audit trail. Phase 9 writes here; nothing rewrites here.

``actor`` distinguishes ENGINE from AI_AGENT. That distinction is the audit
trail's expression of the authority boundary — an ai_agent actor may appear on
a recommendation entry and must never appear on an execution entry. The
constraint below enforces it in the database rather than trusting callers.

Snapshots are stored through ``app.core.security.redact()``, so a secret cannot
enter the audit trail even when the surrounding payload carries one.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ActorType
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, enum_check

if TYPE_CHECKING:  # runtime resolution goes through SQLAlchemy's class registry
    from app.models.recovery_case import RecoveryCase


class AuditEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only. Never updated, never deleted outside case cascade."""

    __tablename__ = "audit_events"

    case_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    actor: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    input_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    #: True when this entry records an execution of a money-moving action.
    #: Constrained so that an ai_agent can never be the actor of one.
    is_execution: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    case: Mapped["RecoveryCase | None"] = relationship(back_populates="audit_events")

    __table_args__ = (
        enum_check("actor", ActorType.values(), name="actor_valid"),
        # The authority boundary, enforced in the database.
        CheckConstraint(
            "NOT is_execution OR actor IN ('engine', 'human', 'system')",
            name="execution_actor_never_ai",
        ),
        Index("ix_audit_events_case_created", "case_id", "created_at"),
    )
