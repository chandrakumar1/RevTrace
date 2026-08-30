"""Deterministic stratified randomisation.

The arm a risk lands in is a **pure function of its identity**:

    bucket = int(sha256(f"{risk_id}:{experiment_id}:{salt}")[:8], 16) % 10000
    arm    = holdout if bucket < holdout_bps else treatment

Hashing rather than drawing buys two properties from one line. The assignment is
**reproducible** — an auditor recomputes it from stored inputs and confirms it —
and it is **idempotent**, because detection upserts risks on the natural key
`(merchant_id, order_id, risk_type)`, so a duplicate, delayed, or out-of-order
webhook that re-triggers detection yields the same `risk_id` and therefore the
same arm. Randomisation and webhook idempotency are satisfied together.

A `random()` draw would satisfy neither. It would also make the holdout
unverifiable: nobody could check after the fact that a disappointing unit had
not been quietly moved.

**Strata are `risk_type` and `amount_band`.** Both are stored on every risk. An
earlier draft added `payment_method`, `issuer` and `customer_tier`; auditing the
schema before locking showed the last two exist nowhere, and `payment_method` is
definitionally absent for two of the four detectable risk types — a checkout
abandonment has no payment attempt, and a subscription failure has no order to
read one from. `payment_method` survives as a balance and subgroup covariate,
which is where a missing value can be reported rather than silently pooled.

`reconciliation_mismatch` is never assigned: its amount at risk is zero by
definition (ADR 0007), so it has nothing to recover and would drag the effect
estimate toward zero.

Nothing here reads a clock. `as_of` is supplied, as it is everywhere else in
this codebase, so an assignment's timestamp is reproducible.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaseAssignment, RevenueRisk
from app.models.enums import Arm, DecisionType, ExperimentStatus, RiskType
from app.models.experiment import Experiment

#: Full basis-point scale. `holdout_bps` is compared against a bucket in 0..9999.
BPS_SCALE = 10_000

#: Bytes of the digest used for the bucket. Eight hex characters is 32 bits,
#: far more resolution than 10,000 buckets need.
_BUCKET_HEX_CHARS = 8

#: Risk types that are never randomised.
#:
#: `reconciliation_mismatch` carries zero amount at risk — the money arrived and
#: only the bookkeeping is broken — so there is nothing for an intervention to
#: recover. `payment_degradation` has no detector and cannot arise; it is listed
#: so that adding one later forces a deliberate decision here.
EXCLUDED_RISK_TYPES: frozenset[str] = frozenset(
    {
        RiskType.RECONCILIATION_MISMATCH.value,
        RiskType.PAYMENT_DEGRADATION.value,
    }
)

#: Amount bands in minor units, as pre-registered. The 15,00,000 paise
#: (Rs 15,000) boundary is the RBI additional-factor-authentication threshold
#: for recurring debits, so cases either side of it face different regulatory
#: constraints and must not be pooled.
AMOUNT_BANDS: tuple[tuple[str, int], ...] = (
    ("<500", 50_000),
    ("500-2000", 200_000),
    ("2000-5000", 500_000),
    ("5000-15000", 1_500_000),
)
TOP_AMOUNT_BAND = ">15000"

#: Audit action recorded when a risk is assigned.
ASSIGN_ACTION = "EXPERIMENT_ASSIGN"


class AssignmentError(ValueError):
    """A risk could not be assigned, and the reason is the caller's to fix."""


def amount_band(amount_minor: int) -> str:
    """The pre-registered band for an amount in minor units."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise AssignmentError(
            f"amount_minor must be an integer count of minor units, "
            f"got {type(amount_minor).__name__}"
        )
    if amount_minor < 0:
        raise AssignmentError(f"amount_minor must be non-negative, got {amount_minor}")

    for label, upper_exclusive in AMOUNT_BANDS:
        if amount_minor < upper_exclusive:
            return label
    return TOP_AMOUNT_BAND


def stratum_key(risk_type: str, amount_minor: int) -> str:
    """`risk_type|amount_band` — the two covariates every risk actually has."""
    if risk_type not in RiskType.values():
        raise AssignmentError(f"unknown risk_type {risk_type!r}")
    return f"{risk_type}|{amount_band(amount_minor)}"


def assignment_digest(risk_id: uuid.UUID, experiment_id: uuid.UUID, salt: str) -> str:
    """The full hex digest the arm is derived from.

    Stored on the assignment so the draw can be re-verified independently, which
    is the difference between a randomisation an auditor can check and one they
    have to take on trust.
    """
    if not salt or not salt.strip():
        raise AssignmentError("assignment salt must not be empty")
    return hashlib.sha256(f"{risk_id}:{experiment_id}:{salt}".encode()).hexdigest()


def bucket_for(digest: str) -> int:
    """Map a digest into 0..9999. Integer arithmetic only."""
    return int(digest[:_BUCKET_HEX_CHARS], 16) % BPS_SCALE


def arm_for_bucket(bucket: int, holdout_bps: int) -> Arm:
    """Below the holdout share is holdout; at or above it is treatment.

    Strictly `<` so that `holdout_bps = 0` would assign nobody to the holdout,
    and `10000` everybody — both rejected upstream by a CHECK constraint, but
    the boundary behaviour should still be the obvious one.
    """
    if not 0 <= bucket < BPS_SCALE:
        raise AssignmentError(f"bucket {bucket} outside 0..{BPS_SCALE - 1}")
    return Arm.HOLDOUT if bucket < holdout_bps else Arm.TREATMENT


@dataclass(frozen=True, slots=True)
class AssignmentDecision:
    """One computed assignment, before it is written.

    Pure: computing this touches no database, so the arm can be recomputed and
    checked without one.
    """

    risk_id: uuid.UUID
    experiment_id: uuid.UUID
    arm: Arm
    stratum_key: str
    assignment_hash: str
    bucket: int

    @property
    def is_holdout(self) -> bool:
        return self.arm is Arm.HOLDOUT


def decide(
    risk_id: uuid.UUID,
    experiment_id: uuid.UUID,
    *,
    risk_type: str,
    amount_minor: int,
    holdout_bps: int,
    salt: str,
) -> AssignmentDecision:
    """Compute an arm. Pure — no database, no clock, no randomness."""
    if not 0 < holdout_bps < BPS_SCALE:
        raise AssignmentError(
            f"holdout_bps must be within 1..{BPS_SCALE - 1}, got {holdout_bps}; "
            "0 or full scale would leave one arm empty, which is not an experiment"
        )

    digest = assignment_digest(risk_id, experiment_id, salt)
    bucket = bucket_for(digest)

    return AssignmentDecision(
        risk_id=risk_id,
        experiment_id=experiment_id,
        arm=arm_for_bucket(bucket, holdout_bps),
        stratum_key=stratum_key(risk_type, amount_minor),
        assignment_hash=digest,
        bucket=bucket,
    )


def is_eligible(risk: RevenueRisk) -> bool:
    """Whether a risk may enter the experiment at all."""
    return risk.risk_type not in EXCLUDED_RISK_TYPES


@dataclass(frozen=True, slots=True)
class AssignmentRunSummary:
    """What one assignment pass did."""

    experiment_id: uuid.UUID
    merchant_id: uuid.UUID
    as_of: datetime
    risks_examined: int = 0
    assigned: int = 0
    already_assigned: int = 0
    excluded: int = 0
    treatment: int = 0
    holdout: int = 0

    @property
    def newly_assigned(self) -> int:
        return self.treatment + self.holdout


def existing_assignment(
    session: Session, risk_id: uuid.UUID, experiment_id: uuid.UUID
) -> CaseAssignment | None:
    statement = select(CaseAssignment).where(
        CaseAssignment.risk_id == risk_id,
        CaseAssignment.experiment_id == experiment_id,
    )
    return session.execute(statement).scalars().first()


def _require_running(experiment: Experiment) -> None:
    """Only a running experiment may enrol.

    A DRAFT experiment's specification can still change, and enrolling against a
    specification that might move is the thing pre-registration exists to
    prevent. A CLOSED one has a fixed horizon, and admitting a late unit would
    break it.
    """
    if experiment.status != ExperimentStatus.RUNNING.value:
        raise AssignmentError(
            f"experiment {experiment.id} is {experiment.status}; only a running "
            "experiment may enrol risks"
        )


def assign_risk(
    session: Session,
    experiment: Experiment,
    risk: RevenueRisk,
    as_of: datetime,
    *,
    salt: str,
) -> CaseAssignment | None:
    """Assign one risk, or return the assignment it already has.

    Idempotent by construction: the arm derives from identity, and
    `UNIQUE (risk_id, experiment_id)` is the storage-layer backstop. Re-running
    detection and re-assigning is a no-op rather than a second row.

    Returns `None` for a risk the experiment excludes.
    """
    _require_running(experiment)

    if as_of.tzinfo is None:
        raise AssignmentError("as_of must be timezone-aware")

    if not is_eligible(risk):
        return None

    found = existing_assignment(session, risk.id, experiment.id)
    if found is not None:
        return found

    decision = decide(
        risk.id,
        experiment.id,
        risk_type=risk.risk_type,
        amount_minor=risk.amount_at_risk,
        holdout_bps=experiment.holdout_bps,
        salt=salt,
    )

    assignment = CaseAssignment(
        risk_id=decision.risk_id,
        experiment_id=decision.experiment_id,
        arm=decision.arm.value,
        stratum_key=decision.stratum_key,
        assignment_hash=decision.assignment_hash,
        assigned_at=as_of,
    )
    session.add(assignment)
    session.flush()
    return assignment


def audit_entry_for(decision: AssignmentDecision, as_of: datetime) -> dict[str, object]:
    """The audit payload for one assignment.

    Anchored by `risk_id`, because an assignment happens before any recovery
    case exists — and a holdout risk never gets one at all.

    The numeric snapshot carries the bucket, the threshold, and the digest, so a
    reviewer can recompute the arm by hand rather than trusting the outcome.
    """
    return {
        "risk_id": decision.risk_id,
        "actor": "engine",
        "action": ASSIGN_ACTION,
        "decision_type": DecisionType.ASSIGN.value,
        "is_execution": False,
        "numeric_snapshot": {
            "arm": decision.arm.value,
            "bucket": decision.bucket,
            "stratum_key": decision.stratum_key,
            "assignment_hash": decision.assignment_hash,
            "assigned_at": as_of.isoformat(),
        },
    }


def assign_for_merchant(
    session: Session,
    experiment: Experiment,
    merchant_id: uuid.UUID,
    as_of: datetime,
    *,
    salt: str,
    risks: Sequence[RevenueRisk] | None = None,
) -> AssignmentRunSummary:
    """Assign every eligible risk for one merchant.

    Reads risks in a stable order so a pass is reproducible. Assignment writes
    to `case_assignments` only — it creates no recovery case, takes no action,
    and moves no money.
    """
    _require_running(experiment)

    if as_of.tzinfo is None:
        raise AssignmentError("as_of must be timezone-aware")

    if risks is None:
        statement = (
            select(RevenueRisk)
            .where(RevenueRisk.merchant_id == merchant_id)
            .order_by(RevenueRisk.detected_at, RevenueRisk.risk_type, RevenueRisk.id)
        )
        risks = list(session.execute(statement).scalars())

    assigned = already = excluded = treatment = holdout = 0

    for risk in risks:
        if not is_eligible(risk):
            excluded += 1
            continue

        if existing_assignment(session, risk.id, experiment.id) is not None:
            already += 1
            continue

        assignment = assign_risk(session, experiment, risk, as_of, salt=salt)
        if assignment is None:  # pragma: no cover - guarded by is_eligible above
            excluded += 1
            continue

        assigned += 1
        if assignment.arm == Arm.HOLDOUT.value:
            holdout += 1
        else:
            treatment += 1

    return AssignmentRunSummary(
        experiment_id=experiment.id,
        merchant_id=merchant_id,
        as_of=as_of,
        risks_examined=len(risks),
        assigned=assigned,
        already_assigned=already,
        excluded=excluded,
        treatment=treatment,
        holdout=holdout,
    )


__all__ = [
    "AMOUNT_BANDS",
    "ASSIGN_ACTION",
    "BPS_SCALE",
    "EXCLUDED_RISK_TYPES",
    "TOP_AMOUNT_BAND",
    "AssignmentDecision",
    "AssignmentError",
    "AssignmentRunSummary",
    "amount_band",
    "arm_for_bucket",
    "assign_for_merchant",
    "assign_risk",
    "assignment_digest",
    "audit_entry_for",
    "bucket_for",
    "decide",
    "existing_assignment",
    "is_eligible",
    "stratum_key",
]
