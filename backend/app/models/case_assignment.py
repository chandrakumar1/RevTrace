"""Case assignment — which arm a detected risk was randomised into.

**The unit of randomisation is a `revenue_risk`, not a `recovery_case`.** The
revised plan calls it a "case", and a case here *is* a detected revenue risk on
a single order — the thing that exists the moment detection fires.

That choice is what makes intention-to-treat sound. A `recovery_case` is a
Phase 6 artefact created when the pipeline decides to do something; anchoring
assignment to it would mean a unit that never got that far had no assignment and
silently left the denominator, which is selection bias of exactly the kind ITT
exists to prevent. Anchoring to the risk fixes the analysis population at
randomisation and nothing downstream can shrink it.

It also keeps detection honest: `recovery_cases` stays empty until something is
actually recovered, which seven existing tests assert and which is the
project's central architectural claim.

Append-only by design: no `updated_at`, no update path. An arm that could be
changed after the fact is not a randomisation, and re-labelling a treated unit
as a control is the easiest way to manufacture a result.

`assignment_hash` records the digest the arm was derived from, so any auditor
can recompute it from `(risk_id, experiment_id, salt)`. It is also why a
duplicate or out-of-order webhook that re-triggers detection lands in the same
arm: the assignment is a pure function of identity, not a coin flip.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import Arm
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, enum_check


class CaseAssignment(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only. Never updated, never deleted outside the risk cascade."""

    __tablename__ = "case_assignments"

    #: The detected risk this assignment belongs to. Identity is stable across
    #: re-runs because detection upserts on the natural key
    #: (merchant_id, order_id, risk_type) rather than inserting a second row.
    risk_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("revenue_risks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    arm: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    #: The covariate combination this risk was stratified into.
    stratum_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    #: Hex digest the arm was derived from, so the draw can be re-verified.
    assignment_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # One risk is assigned exactly once per experiment, enforced by storage
        # rather than by the caller remembering to check.
        UniqueConstraint("risk_id", "experiment_id", name="uq_case_assignments_risk_experiment"),
        enum_check("arm", Arm.values(), name="arm_valid"),
        CheckConstraint("char_length(stratum_key) > 0", name="stratum_key_not_blank"),
        CheckConstraint("char_length(assignment_hash) > 0", name="assignment_hash_not_blank"),
        Index("ix_case_assignments_experiment_arm", "experiment_id", "arm"),
        Index("ix_case_assignments_experiment_stratum", "experiment_id", "stratum_key"),
    )
