"""The seam where the incrementality gate meets the database.

Deliberately thin, in the same shape as `services/detection/service.py`: it
loads what the pure gate needs, calls it, and persists the result. It contains
no decision logic of its own — every rule lives in `app.engine.policy_engine`,
where it can be tested without a session.

**An abstention creates no recovery case.** It writes one row to `audit_events`,
anchored on `risk_id`. Creating a case to hold a non-action would mean inventing
`expected_recovery`, `max_cost`, `estimated_cost`, `net_expected_recovery` and a
strategy that nobody computed, so that a NOT NULL constraint could be satisfied
for a decision that spends nothing.

**An ACT decision persists nothing here.** Execution, its recovery case and its
approval trail belong to a later step that does not exist yet; a seam that
quietly created an approved case would cross the authority boundary this
project exists to hold. The decision is returned to the caller instead.

**`policy_status` is not touched.** `CaseDecision` is authoritative for this
gate and only this gate; see the boundary note in `app.engine.policy_engine`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.policy_engine import (
    ABSTAIN_ACTION,
    GateDecision,
    InterventionTerms,
    PolicyError,
    UpliftEvidence,
    decide,
)
from app.models import CaseAssignment
from app.models.audit_event import AuditEvent
from app.models.enums import Arm, CaseDecision, Quadrant
from app.models.intervention import Intervention
from app.models.uplift_score import UpliftScore
from app.repositories.audit_repository import record_abstention


class GateError(ValueError):
    """The gate could not be run for a risk."""


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """What the gate decided, and the audit row if one was written."""

    decision: GateDecision
    audit_event: AuditEvent | None

    @property
    def recorded(self) -> bool:
        return self.audit_event is not None


def load_intervention(session: Session, code: str) -> InterventionTerms:
    """The stored terms for one intervention code."""
    row = session.execute(
        select(Intervention).where(Intervention.code == code)
    ).scalar_one_or_none()
    if row is None:
        raise GateError(f"no intervention with code {code!r}")
    return InterventionTerms.from_row(row)


def load_evidence(
    session: Session,
    risk_id: uuid.UUID,
    experiment_id: uuid.UUID,
    *,
    model_version: str,
) -> UpliftEvidence | None:
    """The stored uplift estimate for one risk, or None when it has none.

    A missing score is not a zero score. Returning None lets the gate abstain
    for insufficient evidence rather than treat an absent estimate as a measured
    null effect.

    `uplift_scores` does not persist the qualification reason, so a unit that
    fell to the global rates is identified by its quadrant: GRAY_ZONE is the
    label the classifier gives when no cell qualified.
    """
    row = session.execute(
        select(UpliftScore).where(
            UpliftScore.risk_id == risk_id,
            UpliftScore.experiment_id == experiment_id,
            UpliftScore.model_version == model_version,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    quadrant = Quadrant(row.quadrant)
    return UpliftEvidence(
        uplift_bps=row.uplift_bps,
        uplift_ci_low_bps=row.uplift_ci_low_bps,
        uplift_ci_high_bps=row.uplift_ci_high_bps,
        quadrant=quadrant,
        qualified=quadrant is not Quadrant.GRAY_ZONE,
    )


def load_arm(session: Session, risk_id: uuid.UUID, experiment_id: uuid.UUID) -> Arm:
    """The arm this risk was randomised into.

    Read from `case_assignments` and never re-derived. Recomputing it here would
    make the decision depend on a salt that might have moved since enrolment,
    which is exactly the failure intention-to-treat exists to prevent.
    """
    row = session.execute(
        select(CaseAssignment.arm).where(
            CaseAssignment.risk_id == risk_id,
            CaseAssignment.experiment_id == experiment_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise GateError(f"risk {risk_id} is not enrolled in experiment {experiment_id}")
    return Arm(row)


def evaluate_risk(
    session: Session,
    risk_id: uuid.UUID,
    experiment_id: uuid.UUID,
    *,
    intervention_code: str,
    model_version: str,
    expected_recovery: int,
    max_cost: int,
    as_of: datetime,
    contacts_this_month: int = 0,
    last_contacted_at: datetime | None = None,
    customer_contactable: bool = True,
    afa_consent: bool = False,
) -> GateOutcome:
    """Run the gate for one risk and persist an abstention if that is the answer.

    Does not commit. The caller owns the transaction, so a run that fails
    part-way leaves nothing behind.
    """
    arm = load_arm(session, risk_id, experiment_id)
    evidence = load_evidence(session, risk_id, experiment_id, model_version=model_version)
    intervention = load_intervention(session, intervention_code)

    decision = decide(
        risk_id,
        experiment_id,
        arm=arm,
        uplift=evidence,
        intervention=intervention,
        expected_recovery=expected_recovery,
        max_cost=max_cost,
        as_of=as_of,
        contacts_this_month=contacts_this_month,
        last_contacted_at=last_contacted_at,
        customer_contactable=customer_contactable,
        afa_consent=afa_consent,
    )

    if decision.decision is not CaseDecision.ABSTAIN:
        return GateOutcome(decision=decision, audit_event=None)

    if decision.reason is None:  # pragma: no cover - GateDecision forbids it
        raise PolicyError("an abstention reached the seam without a reason")

    snapshot = decision.numeric_snapshot()
    snapshot["experiment_id"] = str(experiment_id)
    snapshot["intervention_code"] = intervention.code
    snapshot["arm"] = arm.value
    snapshot["model_version"] = model_version

    event = record_abstention(
        session,
        risk_id=risk_id,
        reason=decision.reason,
        rationale=decision.rationale,
        numeric_snapshot=snapshot,
        as_of=as_of,
        action=ABSTAIN_ACTION,
    )
    return GateOutcome(decision=decision, audit_event=event)


def evaluate_risks(
    session: Session,
    risk_ids: Sequence[uuid.UUID],
    experiment_id: uuid.UUID,
    **kwargs: object,
) -> tuple[GateOutcome, ...]:
    """Run the gate over several risks, in the order given.

    A thin loop rather than a batch: each decision is independent, and nothing
    here depends on how many came before it. That is what makes a replay of the
    same population produce identical rows regardless of ordering or batching.
    """
    return tuple(
        evaluate_risk(session, risk_id, experiment_id, **kwargs)  # type: ignore[arg-type]
        for risk_id in risk_ids
    )


__all__ = [
    "GateError",
    "GateOutcome",
    "evaluate_risk",
    "evaluate_risks",
    "load_arm",
    "load_evidence",
    "load_intervention",
]
