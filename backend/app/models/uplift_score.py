"""Uplift score — one case's estimated treatment effect.

Append-only: a new row per scoring run, keyed by `(experiment_id,
model_version)`. Overwriting would erase which model drove a decision that has
already been taken, and the audit trail needs to be able to answer that.

**Scores belong to an experiment.** A risk can be enrolled in more than one
experiment, and the same model version scores it differently in each, because
cross-fitting derives every cell rate and every fold-local threshold from that
experiment's population alone. Without `experiment_id` two such scores are
indistinguishable once stored, and "which number drove this decision" stops
having an answer. Encoding the experiment into `model_version` was the
alternative and was rejected: it would make the version string mean two things
at once and put a uniqueness guarantee at the mercy of a naming convention.

Everything is integer basis points. `uplift_bps` may be **negative** — that is
the whole point of the sleeping-dog quadrant, where contacting a customer
destroys value. A CHECK that forced it non-negative would define away the
finding this project exists to surface.

`quadrant` is nullable-free but defaults to GRAY_ZONE at the service layer: a
case below the minimum sample per stratum gets no confident label rather than a
confident guess.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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
from app.models.enums import Quadrant
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, enum_check


class UpliftScore(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only. One row per (risk, model version)."""

    __tablename__ = "uplift_scores"

    #: The detected risk this score is about. Anchored to the risk for the same
    #: reason assignments are: every assigned unit must be scoreable, whether or
    #: not a recovery case was ever created for it.
    risk_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("revenue_risks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: The experiment whose population this score was cross-fitted within.
    #: CASCADE matches `case_assignments`: deleting an experiment removes the
    #: enrolment and the estimates derived from it together, since neither means
    #: anything without the design that produced it.
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    model_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: Predicted recovery probability under each arm, in basis points.
    p_treat_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    p_control_bps: Mapped[int] = mapped_column(Integer, nullable=False)

    #: p_treat - p_control. Negative for a sleeping dog, and that is a result,
    #: not an error.
    uplift_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    uplift_ci_low_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    uplift_ci_high_bps: Mapped[int] = mapped_column(Integer, nullable=False)

    quadrant: Mapped[str] = mapped_column(
        String(32), nullable=False, default=Quadrant.GRAY_ZONE.value, index=True
    )

    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # One score per risk per model per experiment. Rescoring the same risk
        # under a new model version still appends; rerunning the *same* model
        # over the same experiment is a duplicate and is refused by storage
        # rather than by the writer remembering to check.
        UniqueConstraint(
            "experiment_id",
            "risk_id",
            "model_version",
            name="uq_uplift_scores_experiment_risk_model",
        ),
        enum_check("quadrant", Quadrant.values(), name="quadrant_valid"),
        CheckConstraint("p_treat_bps >= 0 AND p_treat_bps <= 10000", name="p_treat_bps_in_range"),
        CheckConstraint(
            "p_control_bps >= 0 AND p_control_bps <= 10000", name="p_control_bps_in_range"
        ),
        # A difference of two probabilities lives in [-10000, 10000].
        CheckConstraint("uplift_bps >= -10000 AND uplift_bps <= 10000", name="uplift_bps_in_range"),
        CheckConstraint("uplift_ci_low_bps <= uplift_ci_high_bps", name="uplift_ci_ordered"),
        CheckConstraint(
            "uplift_ci_low_bps <= uplift_bps AND uplift_bps <= uplift_ci_high_bps",
            name="uplift_within_ci",
        ),
        Index("ix_uplift_scores_risk_model", "risk_id", "model_version"),
        Index("ix_uplift_scores_model_quadrant", "model_version", "quadrant"),
    )
