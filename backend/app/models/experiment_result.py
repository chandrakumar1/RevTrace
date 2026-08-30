"""Experiment result — the computed comparison between arms.

Append-only: one row per computation, stamped `computed_at`. Results are never
overwritten, so an interim reading and the final reading both survive and can be
told apart. That matters because the difference between them is exactly what
peeking would hide.

**Three columns may legitimately be negative** and carry no non-negativity
CHECK: `incremental_recovered`, `credited_not_earned`, and
`net_incremental_value`. A treatment that performed worse than its holdout
produces a negative lift, and forbidding that in the schema would define away
the result the project is built to detect.

`gross_recovered`, `action_cost`, and `harm_cost` are counts of money that
actually moved or was actually spent, so those are constrained non-negative.

Rates, effects, and the Qini coefficient are integer basis points; `p_value` is
stored in parts per million so that a p below one basis point still has
resolution. No float columns anywhere (ADR 0001).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, money_column

#: p_value resolution: parts per million, so p = 1e-6 is representable.
P_VALUE_SCALE = 1_000_000


class ExperimentResult(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only. One row per computation of one experiment."""

    __tablename__ = "experiment_results"

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    n_treat: Mapped[int] = mapped_column(Integer, nullable=False)
    n_control: Mapped[int] = mapped_column(Integer, nullable=False)

    rate_treat_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_control_bps: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Average treatment effect and its bootstrap interval, in basis points.
    ate_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    ate_ci_low_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    ate_ci_high_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    p_value_micros: Mapped[int] = mapped_column(Integer, nullable=False)

    # -- the three headline numbers -----------------------------------------
    #: What every competitor reports.
    gross_recovered: Mapped[int] = money_column(nullable=False)
    #: Gross minus what the holdout says would have arrived anyway. May be
    #: negative.
    incremental_recovered: Mapped[int] = money_column(nullable=False)
    #: gross - incremental. The share of the headline that was never caused.
    credited_not_earned: Mapped[int] = money_column(nullable=False)

    # -- do-no-harm ledger ---------------------------------------------------
    harm_ate_bps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    #: The three economic columns are `NULL` when the inputs they need do not
    #: exist. They are assumptions away from being computable, and this project
    #: does not store an assumption as though it were a measurement.
    #:
    #: `harm_cost` needs a monetary value per harm event. The schema records
    #: three kinds — mandate cancellation, opt-out, complaint — and values none
    #: of them; the harm *rate* is measured and reported with an interval, only
    #: its conversion into money is missing.
    harm_cost: Mapped[int | None] = money_column(nullable=True)

    #: `action_cost` needs executed actions. `interventions.unit_cost` is real
    #: and seeded, but `recovery_actions` is empty until Phase 6+ executes
    #: something, and a sum over no rows is an absence rather than a zero.
    action_cost: Mapped[int | None] = money_column(nullable=True)

    #: incremental x margin - action cost - harm cost. May be negative.
    #:
    #: `NULL` while no gross margin exists anywhere in the codebase. Zero would
    #: assert break-even, which is a measured claim nobody made — the same
    #: conflation the nullable Qini coefficient was corrected to avoid.
    net_incremental_value: Mapped[int | None] = money_column(nullable=True)

    #: `NULL` when Q(N) is zero: there was no incremental recovery to apportion,
    #: so the coefficient is undefined. That is a different claim from a
    #: coefficient of zero, which says the ranking did no better than chance.
    #:
    #: `app.causal.qini.qini_coefficient_bps` has always returned `int | None`
    #: for exactly this reason; this column could not express it, and a
    #: `server_default` of 0 would have silently turned every undefined result
    #: into a measurement. Hence nullable, with no default.
    qini_coefficient_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: True when n fell short of the pre-registered plan. The UI must show
    #: "INTERIM - UNDERPOWERED" rather than a number when this is set.
    is_underpowered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    __table_args__ = (
        CheckConstraint("n_treat >= 0 AND n_control >= 0", name="arm_sizes_non_negative"),
        CheckConstraint(
            "rate_treat_bps >= 0 AND rate_treat_bps <= 10000", name="rate_treat_in_range"
        ),
        CheckConstraint(
            "rate_control_bps >= 0 AND rate_control_bps <= 10000", name="rate_control_in_range"
        ),
        CheckConstraint("ate_bps >= -10000 AND ate_bps <= 10000", name="ate_bps_in_range"),
        CheckConstraint("ate_ci_low_bps <= ate_ci_high_bps", name="ate_ci_ordered"),
        CheckConstraint(
            "ate_ci_low_bps <= ate_bps AND ate_bps <= ate_ci_high_bps", name="ate_within_ci"
        ),
        CheckConstraint(
            f"p_value_micros >= 0 AND p_value_micros <= {P_VALUE_SCALE}",
            name="p_value_in_range",
        ),
        CheckConstraint(
            "harm_ate_bps >= -10000 AND harm_ate_bps <= 10000", name="harm_ate_in_range"
        ),
        # NULL-tolerant, in the shape `recovery_cases.actual_recovery` uses: the
        # value is optional, but when present it must be in range. The column
        # carried no CHECK at all before this, so the range is new rather than
        # relaxed.
        CheckConstraint(
            "qini_coefficient_bps IS NULL "
            "OR qini_coefficient_bps >= -10000 AND qini_coefficient_bps <= 10000",
            name="qini_coefficient_in_range",
        ),
        # Money that actually moved or was actually spent cannot be negative.
        CheckConstraint("gross_recovered >= 0", name="gross_recovered_non_negative"),
        CheckConstraint("harm_cost >= 0", name="harm_cost_non_negative"),
        CheckConstraint("action_cost >= 0", name="action_cost_non_negative"),
        # incremental_recovered, credited_not_earned and net_incremental_value
        # are deliberately unconstrained in sign.
        Index("ix_experiment_results_experiment_computed", "experiment_id", "computed_at"),
    )
