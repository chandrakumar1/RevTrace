"""Case outcome — what happened inside the observation window.

`sealed` is the analysis gate. Nothing may read an unsealed outcome for
estimation: a window still open is a number still moving, and analysing it is
peeking. A sweeper closes windows on time and sets the flag; events arriving
after that are logged as late and excluded, which the evaluation report states
rather than quietly absorbing them.

**The `truth_*` columns are simulator ground truth.** They hold both potential
outcomes — what would have happened under treatment *and* under no treatment —
which no real system can observe. They exist so the evaluation report can check
the estimator against a known answer. They are written only by the simulator and
read only by the reporting layer. Nothing under `app/causal/` or `app/engine/`
may reference them, and a test enforces that across the whole application
package. If those columns ever reached the estimator, every number this project
produces would be circular.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, money_column

#: Ground-truth columns. Simulator writes, reporting reads, nothing else may
#: touch them. Kept as a named tuple so the isolation test has one source.
TRUTH_COLUMNS: tuple[str, ...] = (
    "truth_y0",
    "truth_y1",
    "truth_harm_0",
    "truth_harm_1",
    "truth_segment",
)


class CaseOutcome(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "case_outcomes"

    #: The detected risk whose window this measures. Anchored to the risk, not
    #: to a recovery case, so the ITT denominator is fixed at randomisation and
    #: an outcome exists for every assigned unit — including one that was never
    #: acted on.
    risk_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("revenue_risks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    window_opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_closes_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    #: Analysis gate. False means "still moving; do not estimate from this".
    sealed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True
    )
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recovered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: Minor units. Zero when nothing arrived — included in the mean, not dropped.
    recovered_amount: Mapped[int] = money_column(nullable=False)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    contacts_made: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    actions_executed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Execution failed, but the case stays in its assigned arm. Intention-to-
    #: treat is the primary analysis; reclassifying this case as a control
    #: would inflate the measured effect.
    execution_failed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: Harm metrics, estimated with the same causal machinery as recovery.
    harm_mandate_cancelled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    harm_opted_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    harm_complaint: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # -- simulator ground truth; never an estimator input --------------------
    truth_y0: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    truth_y1: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    truth_harm_0: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    truth_harm_1: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    truth_segment: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # One outcome per risk. Two rows would let an analysis silently
        # double-count a recovery.
        UniqueConstraint("risk_id", name="uq_case_outcomes_risk_id"),
        CheckConstraint("recovered_amount >= 0", name="recovered_amount_non_negative"),
        CheckConstraint("contacts_made >= 0", name="contacts_made_non_negative"),
        CheckConstraint("actions_executed >= 0", name="actions_executed_non_negative"),
        CheckConstraint("window_closes_at > window_opens_at", name="window_ordered"),
        # Sealing is a fact with a timestamp, both ways.
        CheckConstraint(
            "sealed = false AND sealed_at IS NULL OR sealed = true AND sealed_at IS NOT NULL",
            name="sealed_requires_timestamp",
        ),
        # Money that arrived must say when, and a recovery of zero is not one.
        CheckConstraint(
            "recovered = false OR recovered_at IS NOT NULL", name="recovered_requires_timestamp"
        ),
        CheckConstraint(
            "recovered = true OR recovered_amount = 0", name="unrecovered_has_no_amount"
        ),
        Index("ix_case_outcomes_sealed_closes", "sealed", "window_closes_at"),
    )
