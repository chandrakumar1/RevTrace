"""Recovery case — one attempt to recover one revenue risk.

`policy_status` and `execution_status` are separate columns on purpose. Policy
approval and execution are distinct gates; collapsing them into one status
would make it possible to represent "executed" without "approved", which is
exactly the state the architecture forbids.

`expected_recovery` is a deterministic engine projection; `actual_recovery` is
the verified outcome. Phase 11 reports both and never substitutes one for the
other.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    AbstainReason,
    Arm,
    CaseDecision,
    ExecutionStatus,
    PolicyStatus,
    RecoveryStrategy,
)
from app.models.mixins import (
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    currency_column,
    enum_check,
    money_column,
)

if TYPE_CHECKING:  # runtime resolution goes through SQLAlchemy's class registry
    from app.models.audit_event import AuditEvent
    from app.models.recovery_action import RecoveryAction
    from app.models.revenue_risk import RevenueRisk


class RecoveryCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "recovery_cases"

    risk_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("revenue_risks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    strategy: Mapped[str] = mapped_column(
        String(64), nullable=False, default=RecoveryStrategy.NO_ACTION.value
    )

    #: Minor units. Deterministic projections from the recovery engine (Phase 6).
    expected_recovery: Mapped[int] = money_column(nullable=False)
    max_cost: Mapped[int] = money_column(nullable=False)
    estimated_cost: Mapped[int] = money_column(nullable=False)
    net_expected_recovery: Mapped[int] = money_column(nullable=False)
    #: Verified outcome (Phase 9). NULL until verification completes.
    actual_recovery: Mapped[int | None] = money_column(nullable=True)

    currency: Mapped[str] = currency_column(nullable=False)

    #: Basis points, 0-10000.
    confidence_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    policy_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PolicyStatus.PENDING.value, index=True
    )
    #: Human-readable reason when policy rejects or escalates. Never silently empty
    #: on a rejection — enforced by the policy engine in Phase 7.
    policy_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    execution_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ExecutionStatus.NOT_STARTED.value, index=True
    )

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- incrementality ledger ----------------------------------------------
    #: Randomised arm, denormalised from `case_assignments` for query
    #: convenience. `case_assignments` remains the append-only record of record;
    #: this column is a read path, not a second source of truth.
    #: NULL means the case predates the experiment or falls outside it.
    arm: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

    #: What the policy gate decided. `abstain` is a real outcome, not an absence
    #: of one, and it is audited as richly as an action.
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

    #: Why acting was declined. Required whenever `decision = 'abstain'`.
    abstain_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    risk: Mapped["RevenueRisk"] = relationship(back_populates="recovery_cases")
    actions: Mapped[list["RecoveryAction"]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("expected_recovery >= 0", name="expected_recovery_non_negative"),
        CheckConstraint("max_cost >= 0", name="max_cost_non_negative"),
        CheckConstraint("estimated_cost >= 0", name="estimated_cost_non_negative"),
        CheckConstraint(
            "actual_recovery IS NULL OR actual_recovery >= 0",
            name="actual_recovery_non_negative",
        ),
        CheckConstraint("estimated_cost <= max_cost", name="estimated_cost_within_max"),
        CheckConstraint(
            "confidence_bps >= 0 AND confidence_bps <= 10000",
            name="confidence_bps_in_range",
        ),
        CheckConstraint("char_length(currency) = 3", name="currency_iso4217"),
        enum_check("strategy", RecoveryStrategy.values(), name="strategy_valid"),
        enum_check("policy_status", PolicyStatus.values(), name="policy_status_valid"),
        enum_check("execution_status", ExecutionStatus.values(), name="execution_status_valid"),
        # The authority boundary, enforced in the database: a case may not reach
        # an executing/executed/verified state unless policy approved it.
        CheckConstraint(
            "execution_status IN ('not_started', 'awaiting_approval', 'aborted', 'failed') "
            "OR policy_status = 'approved'",
            name="execution_requires_policy_approval",
        ),
        Index("ix_recovery_cases_policy_execution", "policy_status", "execution_status"),
        # -- incrementality ledger ------------------------------------------
        CheckConstraint(
            f"arm IS NULL OR arm IN ({', '.join(repr(v) for v in Arm.values())})",
            name="arm_valid",
        ),
        CheckConstraint(
            f"decision IS NULL OR decision IN "
            f"({', '.join(repr(v) for v in CaseDecision.values())})",
            name="decision_valid",
        ),
        CheckConstraint(
            f"abstain_reason IS NULL OR abstain_reason IN "
            f"({', '.join(repr(v) for v in AbstainReason.values())})",
            name="abstain_reason_valid",
        ),
        # An abstention without a reason is an unexplained non-action, which is
        # exactly what this project refuses to ship. The converse is also
        # barred: a reason may not be attached to a decision that acted.
        CheckConstraint(
            "decision IS DISTINCT FROM 'abstain' OR abstain_reason IS NOT NULL",
            name="abstain_requires_reason",
        ),
        CheckConstraint(
            "abstain_reason IS NULL OR decision = 'abstain'",
            name="abstain_reason_requires_abstain",
        ),
        # A holdout case must never be acted on. That is the whole basis of the
        # counterfactual, so the database refuses to store the contradiction.
        CheckConstraint(
            "arm IS DISTINCT FROM 'holdout' OR decision IS DISTINCT FROM 'act'",
            name="holdout_never_acts",
        ),
        Index("ix_recovery_cases_arm_decision", "arm", "decision"),
    )
