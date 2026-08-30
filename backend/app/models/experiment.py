"""Experiment — a pre-registered randomised comparison.

`locked_at` is the pre-registration timestamp and the single most important
column in this table. Once it is set, the hypothesis, metrics, strata, holdout
share, and power parameters are frozen: a result that could have been
re-specified after the data arrived is worth much less than one that could not.

That freeze is enforced in three places, deliberately overlapping:

* a CHECK constraint requiring `locked_at` whenever the status has left DRAFT;
* a database trigger that rejects any UPDATE touching a frozen column while
  `locked_at` is set (see the Day 1 migration);
* `app/experiments/registry.py`, which refuses the transition in application
  code and produces a readable error.

Statistical parameters are stored as **integer basis points**, never floats.
`alpha_bps = 500` is α = 0.05; `power_bps = 8000` is 80% power. The project
bans float columns outright (ADR 0001), and a pre-registered threshold is
exactly the kind of value that must compare exactly across runs.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ExperimentStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check

#: Columns frozen at lock time. The migration's trigger reads this same list;
#: the two are kept in step by a test.
FROZEN_AFTER_LOCK: tuple[str, ...] = (
    "name",
    "hypothesis",
    "primary_metric",
    "secondary_metrics",
    "holdout_bps",
    "strata_definition",
    "planned_n_per_arm",
    "alpha_bps",
    "power_bps",
    "mde_bps",
    "locked_at",
)


class Experiment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "experiments"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)

    primary_metric: Mapped[str] = mapped_column(String(64), nullable=False)
    #: JSONB rather than ARRAY, matching the existing payload/snapshot columns.
    #: Typed as a list because that is what it holds — a JSONB column can carry
    #: either, and declaring `dict` here would make every write a type error.
    secondary_metrics: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    #: Share of cases held out, in basis points. 5000 = 50/50 allocation.
    holdout_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Which covariates define a stratum, e.g. risk type x amount band x method.
    strata_definition: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    planned_n_per_arm: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Significance level, power, and minimum detectable effect — basis points.
    alpha_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    power_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=8000)
    mde_bps: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ExperimentStatus.DRAFT.value, index=True
    )

    #: The pre-registration moment. NULL while DRAFT; immutable once set.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        enum_check("status", ExperimentStatus.values(), name="status_valid"),
        CheckConstraint("char_length(name) > 0", name="name_not_blank"),
        CheckConstraint("char_length(hypothesis) > 0", name="hypothesis_not_blank"),
        # A holdout of 0 or 10000 leaves one arm empty, which is not an
        # experiment. Both ends are excluded on purpose.
        CheckConstraint("holdout_bps > 0 AND holdout_bps < 10000", name="holdout_bps_in_range"),
        CheckConstraint("alpha_bps > 0 AND alpha_bps < 10000", name="alpha_bps_in_range"),
        CheckConstraint("power_bps > 0 AND power_bps < 10000", name="power_bps_in_range"),
        CheckConstraint("mde_bps > 0 AND mde_bps <= 10000", name="mde_bps_in_range"),
        CheckConstraint("planned_n_per_arm > 0", name="planned_n_positive"),
        # Anything past DRAFT must carry its pre-registration timestamp. This is
        # what makes "locked" a fact about the row rather than a claim about it.
        CheckConstraint(
            "status = 'draft' OR locked_at IS NOT NULL",
            name="non_draft_requires_lock",
        ),
        CheckConstraint(
            "status = 'draft' AND locked_at IS NULL OR status <> 'draft'",
            name="draft_has_no_lock",
        ),
        CheckConstraint(
            "started_at IS NULL OR locked_at IS NOT NULL AND started_at >= locked_at",
            name="started_after_locked",
        ),
        CheckConstraint(
            "closed_at IS NULL OR started_at IS NULL OR closed_at >= started_at",
            name="closed_after_started",
        ),
        CheckConstraint(
            "status <> 'closed' OR closed_at IS NOT NULL", name="closed_requires_timestamp"
        ),
        Index("ix_experiments_status_locked", "status", "locked_at"),
    )
