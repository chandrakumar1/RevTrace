"""Loading the analysis population, and refusing when it is not ready.

This is the seam between the database and the estimators. Everything below it
is pure arithmetic over lists of integers; everything above it is storage. The
seam exists so the estimators never learn what a session is, and so the one
judgement that has to be made about data readiness is made in exactly one
place.

**The refusal is the point.** An experiment is analysed when every enrolled
assignment has a *sealed* outcome, and not before. Two failures are refused
here rather than worked around:

* an assignment with **no outcome row** — the unit was randomised and then lost
  track of, so any estimate would silently drop it;
* an assignment whose outcome is **not sealed** — the window is still open, the
  number is still moving, and reading it is peeking.

The tempting alternative in both cases is to analyse whatever is ready. That is
precisely the thing a fixed horizon exists to prevent: units that resolve
quickly are not a random sample of units, so an estimate over "whatever has
finished" is biased toward fast recoveries no matter how large it gets.
Refusing loudly is the only honest option, and the exception carries the counts
so the caller can say what is missing.

**Two populations come out.**

*Intention-to-treat* is primary: every unit is counted in the arm it was
assigned, including units whose execution failed. Moving a failed execution
into the control arm would inflate the measured effect, which is why the arm is
read from `case_assignments` and never re-derived from what happened.

*Per-protocol* is secondary and reported alongside, with the difference stated.
It drops units that did not receive their assigned condition: a treated unit
whose execution failed, and a held-out unit that was acted on anyway. The
pre-registration names `execution_failed` as the non-compliance marker, so that
is what is used — `actions_executed == 0` is deliberately *not* treated as
non-compliance, because it cannot be told apart from an action that has simply
not been attempted yet.

Nothing here writes, and nothing here reads a `truth_*` column. The generator's
answer key reaches the evaluation reporter by a different path entirely.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaseAssignment, CaseOutcome
from app.models.enums import Arm

#: Full basis-point scale.
BPS_SCALE = 10_000

#: Names for the two populations, used in reports and payloads.
ITT = "itt"
PER_PROTOCOL = "per_protocol"


class AnalysisError(ValueError):
    """The analysis population could not be built."""


class AnalysisRefused(AnalysisError):
    """The data is not ready, and estimating from it would be misleading.

    Carries the counts rather than only a message: a caller reporting "3
    windows still open" is useful, and "something went wrong" is not.
    """

    def __init__(
        self,
        message: str,
        *,
        experiment_id: uuid.UUID,
        missing_outcomes: int = 0,
        unsealed_outcomes: int = 0,
    ) -> None:
        super().__init__(message)
        self.experiment_id = experiment_id
        self.missing_outcomes = missing_outcomes
        self.unsealed_outcomes = unsealed_outcomes


@dataclass(frozen=True, slots=True)
class OutcomeRow:
    """One randomised unit and its sealed outcome.

    Narrow on purpose. The estimator layer is handed integers; this is the only
    type that knows both an arm and an outcome, and it carries no covariate, no
    amount at risk, and no ground truth.
    """

    risk_id: uuid.UUID
    arm: str
    stratum_key: str
    recovered: bool
    recovered_amount: int
    execution_failed: bool
    actions_executed: int
    contacts_made: int
    harm_mandate_cancelled: bool
    harm_opted_out: bool
    harm_complaint: bool

    @property
    def is_treatment(self) -> bool:
        return self.arm == Arm.TREATMENT.value

    @property
    def is_compliant(self) -> bool:
        """Whether this unit actually received its assigned condition.

        A treated unit whose execution failed did not get the treatment. A
        held-out unit that was acted on did not get the control. Both are
        protocol violations, and both stay in the ITT population regardless.
        """
        if self.is_treatment:
            return not self.execution_failed
        return self.actions_executed == 0


@dataclass(frozen=True, slots=True)
class ArmSample:
    """One arm, reduced to the integer sequences the estimators consume.

    Parallel tuples rather than a list of objects: every estimator takes a
    sequence of integers, and keeping them aligned by index means a unit's
    recovery, amount and harm flags can never drift apart.
    """

    arm: str
    risk_ids: tuple[uuid.UUID, ...]
    recovered: tuple[int, ...]
    amounts: tuple[int, ...]
    harm_mandate_cancelled: tuple[int, ...]
    harm_opted_out: tuple[int, ...]
    harm_complaint: tuple[int, ...]

    def __post_init__(self) -> None:
        sizes = {
            len(self.risk_ids),
            len(self.recovered),
            len(self.amounts),
            len(self.harm_mandate_cancelled),
            len(self.harm_opted_out),
            len(self.harm_complaint),
        }
        if len(sizes) != 1:
            raise AnalysisError(f"arm {self.arm} has misaligned columns: {sorted(sizes)}")

    @property
    def n(self) -> int:
        return len(self.risk_ids)

    @property
    def recoveries(self) -> int:
        return sum(self.recovered)

    @property
    def gross(self) -> int:
        """Money that arrived in this arm, in minor units."""
        return sum(self.amounts)

    @property
    def is_empty(self) -> bool:
        return self.n == 0


def _arm_sample(arm: str, rows: Sequence[OutcomeRow]) -> ArmSample:
    return ArmSample(
        arm=arm,
        risk_ids=tuple(row.risk_id for row in rows),
        recovered=tuple(int(row.recovered) for row in rows),
        amounts=tuple(row.recovered_amount for row in rows),
        harm_mandate_cancelled=tuple(int(row.harm_mandate_cancelled) for row in rows),
        harm_opted_out=tuple(int(row.harm_opted_out) for row in rows),
        harm_complaint=tuple(int(row.harm_complaint) for row in rows),
    )


@dataclass(frozen=True, slots=True)
class AnalysisSample:
    """Both arms of one population, ready for the estimators."""

    experiment_id: uuid.UUID
    analysis: str
    treatment: ArmSample
    holdout: ArmSample
    excluded_treatment: int = 0
    excluded_holdout: int = 0

    @property
    def n_total(self) -> int:
        return self.treatment.n + self.holdout.n

    @property
    def excluded_total(self) -> int:
        return self.excluded_treatment + self.excluded_holdout

    @property
    def non_compliance_bps(self) -> int:
        """Share of the randomised population dropped, in basis points.

        Zero for ITT by construction — that is what makes it intention-to-treat
        — and the figure the pre-registration requires alongside per-protocol.
        """
        enrolled = self.n_total + self.excluded_total
        if enrolled == 0:
            return 0
        return (self.excluded_total * BPS_SCALE + enrolled // 2) // enrolled

    def as_dict(self) -> dict[str, object]:
        return {
            "experiment_id": str(self.experiment_id),
            "analysis": self.analysis,
            "n_treatment": self.treatment.n,
            "n_holdout": self.holdout.n,
            "n_total": self.n_total,
            "recoveries_treatment": self.treatment.recoveries,
            "recoveries_holdout": self.holdout.recoveries,
            "gross_treatment": self.treatment.gross,
            "gross_holdout": self.holdout.gross,
            "excluded_treatment": self.excluded_treatment,
            "excluded_holdout": self.excluded_holdout,
            "non_compliance_bps": self.non_compliance_bps,
        }


# -- loading --------------------------------------------------------------


def load_outcome_rows(session: Session, experiment_id: uuid.UUID) -> list[OutcomeRow]:
    """Every enrolled unit with its sealed outcome, ordered by `risk_id`.

    Refuses unless the population is complete and closed. The order is by
    `risk_id` so two loads of the same data produce byte-identical sequences —
    which matters because the bootstrap draws from these, and an interval that
    moved with row order would not be reproducible.

    An outer join is used deliberately: an inner join would make a missing
    outcome invisible by simply returning fewer rows, which is exactly the
    silent drop this function exists to prevent.
    """
    statement = (
        select(CaseAssignment, CaseOutcome)
        .outerjoin(CaseOutcome, CaseOutcome.risk_id == CaseAssignment.risk_id)
        .where(CaseAssignment.experiment_id == experiment_id)
        .order_by(CaseAssignment.risk_id)
    )
    pairs = list(session.execute(statement).all())

    missing = [assignment.risk_id for assignment, outcome in pairs if outcome is None]
    unsealed = [
        assignment.risk_id
        for assignment, outcome in pairs
        if outcome is not None and not outcome.sealed
    ]

    if missing or unsealed:
        raise AnalysisRefused(
            _refusal_message(len(pairs), missing, unsealed),
            experiment_id=experiment_id,
            missing_outcomes=len(missing),
            unsealed_outcomes=len(unsealed),
        )

    return [
        OutcomeRow(
            risk_id=assignment.risk_id,
            arm=assignment.arm,
            stratum_key=assignment.stratum_key,
            recovered=outcome.recovered,
            recovered_amount=outcome.recovered_amount,
            execution_failed=outcome.execution_failed,
            actions_executed=outcome.actions_executed,
            contacts_made=outcome.contacts_made,
            harm_mandate_cancelled=outcome.harm_mandate_cancelled,
            harm_opted_out=outcome.harm_opted_out,
            harm_complaint=outcome.harm_complaint,
        )
        for assignment, outcome in pairs
    ]


def _refusal_message(
    enrolled: int,
    missing: Sequence[uuid.UUID],
    unsealed: Sequence[uuid.UUID],
) -> str:
    """A refusal a reader can act on, naming a few of the offending units."""
    parts: list[str] = []
    if missing:
        parts.append(
            f"{len(missing)} of {enrolled} enrolled assignments have no outcome row "
            f"(for example {_examples(missing)})"
        )
    if unsealed:
        parts.append(
            f"{len(unsealed)} of {enrolled} outcomes are not sealed "
            f"(for example {_examples(unsealed)})"
        )
    return (
        "; ".join(parts) + ". Analysing a partial population would measure units "
        "that resolved quickly rather than a random sample of units. Seal every "
        "window first."
    )


def _examples(ids: Sequence[uuid.UUID], limit: int = 3) -> str:
    shown = ", ".join(str(value) for value in ids[:limit])
    return shown if len(ids) <= limit else f"{shown}, ..."


# -- populations ----------------------------------------------------------


def itt_sample(rows: Sequence[OutcomeRow], experiment_id: uuid.UUID) -> AnalysisSample:
    """Intention-to-treat: every unit in the arm it was assigned.

    Nothing is excluded. A unit whose execution failed stays in `treatment`,
    which is the whole content of the phrase — reclassifying it would let a
    treatment look better precisely where it worked least.
    """
    return AnalysisSample(
        experiment_id=experiment_id,
        analysis=ITT,
        treatment=_arm_sample(Arm.TREATMENT.value, [r for r in rows if r.is_treatment]),
        holdout=_arm_sample(Arm.HOLDOUT.value, [r for r in rows if not r.is_treatment]),
    )


def per_protocol_sample(rows: Sequence[OutcomeRow], experiment_id: uuid.UUID) -> AnalysisSample:
    """Per-protocol: only units that received their assigned condition.

    Secondary by design. Dropping non-compliant units breaks randomisation —
    the units that fail to receive treatment are not a random subset — so this
    is reported *beside* the ITT figure with the difference stated, never in
    place of it.
    """
    treated = [row for row in rows if row.is_treatment]
    held_out = [row for row in rows if not row.is_treatment]

    compliant_treated = [row for row in treated if row.is_compliant]
    compliant_held_out = [row for row in held_out if row.is_compliant]

    return AnalysisSample(
        experiment_id=experiment_id,
        analysis=PER_PROTOCOL,
        treatment=_arm_sample(Arm.TREATMENT.value, compliant_treated),
        holdout=_arm_sample(Arm.HOLDOUT.value, compliant_held_out),
        excluded_treatment=len(treated) - len(compliant_treated),
        excluded_holdout=len(held_out) - len(compliant_held_out),
    )


@dataclass(frozen=True, slots=True)
class AnalysisPopulation:
    """Both populations from one load, so the pair is always consistent."""

    experiment_id: uuid.UUID
    rows: tuple[OutcomeRow, ...]
    itt: AnalysisSample
    per_protocol: AnalysisSample

    @property
    def n_enrolled(self) -> int:
        return len(self.rows)


def build_population(
    rows: Sequence[OutcomeRow],
    experiment_id: uuid.UUID,
    *,
    require_both_arms: bool = True,
) -> AnalysisPopulation:
    """Both populations from already-loaded rows. Pure.

    Refuses an empty arm by default. A comparison needs something to compare,
    and failing here names the problem far better than the same failure would
    surfacing from inside a bootstrap resample.
    """
    itt = itt_sample(rows, experiment_id)
    per_protocol = per_protocol_sample(rows, experiment_id)

    if require_both_arms and (itt.treatment.is_empty or itt.holdout.is_empty):
        raise AnalysisRefused(
            f"experiment {experiment_id} has {itt.treatment.n} treated and "
            f"{itt.holdout.n} held-out units; an arm with no units is not an experiment",
            experiment_id=experiment_id,
        )

    return AnalysisPopulation(
        experiment_id=experiment_id,
        rows=tuple(rows),
        itt=itt,
        per_protocol=per_protocol,
    )


def load_population(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    require_both_arms: bool = True,
) -> AnalysisPopulation:
    """Load and build. Reads only; writes nothing."""
    return build_population(
        load_outcome_rows(session, experiment_id),
        experiment_id,
        require_both_arms=require_both_arms,
    )


__all__ = [
    "BPS_SCALE",
    "ITT",
    "PER_PROTOCOL",
    "AnalysisError",
    "AnalysisPopulation",
    "AnalysisRefused",
    "AnalysisSample",
    "ArmSample",
    "OutcomeRow",
    "build_population",
    "itt_sample",
    "load_outcome_rows",
    "load_population",
    "per_protocol_sample",
]
