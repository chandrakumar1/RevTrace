"""Uplift-score persistence.

`uplift_scores` is the **only** table this module writes to. It cannot reach
`experiment_results`, `recovery_cases`, `recovery_actions` or `audit_events`:
scoring is an estimate, not a decision, and certainly not an approval.

**The batch is the unit, not the row.** Every check runs against the whole
batch before a single `session.add`, and any failure refuses all of it. A
half-written scoring run is worse than none — the missing half is invisible,
so the analysis that reads it later would silently be computed over a
population nobody chose.

**The interval is never adjusted to fit.** `ck_uplift_scores_uplift_within_ci`
requires `ci_low <= uplift <= ci_high`, and a percentile bootstrap does not
mathematically guarantee that: the estimate is a difference of two rates while
the bounds are order statistics of resampled differences, and with enough ties
or a tiny cell the point estimate can fall outside its own interval. When that
happens the batch is refused and the numbers are reported as they came. Clipping
a bound to satisfy a CHECK would make the stored interval a fiction, and an
interval that has been quietly widened to contain its estimate no longer means
what a confidence interval means. The refusal is the finding.

The same guards exist in the database as CHECKs. Both are kept deliberately:
the CHECK holds against a psql session and a future migration, and the guard
here turns a mid-flush `IntegrityError` naming one row into an error that names
the batch and every problem in it.

Two classes are called `UpliftScore` — the causal layer's frozen dataclass and
the ORM row. They are imported under distinct names below, and converting one
into the other explicitly is this module's whole job.

Identity is `(experiment_id, risk_id, model_version)`, enforced by a unique
constraint. The pre-insert duplicate check is **sequential only**: a concurrent
transaction's uncommitted rows are invisible to this session's SELECT, so two
simultaneous runs of the same model could both pass it. The constraint is what
actually holds under concurrency; the check exists to turn the common case into
a legible refusal rather than an integrity error. Scoring runs are single-process
today, the same limitation `risk_repository` documents for detection.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.causal.quadrants import QuadrantAssignment
from app.causal.uplift import UpliftScore as CausalUpliftScore
from app.models import CaseAssignment
from app.models.enums import Quadrant
from app.models.uplift_score import UpliftScore as UpliftScoreRow

#: Basis-point bounds, mirroring the table's CHECK constraints.
BPS_SCALE = 10_000
RATE_MIN, RATE_MAX = 0, BPS_SCALE
UPLIFT_MIN, UPLIFT_MAX = -BPS_SCALE, BPS_SCALE

#: How many individual problems an error message lists before summarising. A
#: ten-thousand-row batch with a systematic fault would otherwise produce an
#: unreadable exception.
MAX_REPORTED_PROBLEMS = 5


class UpliftPersistenceError(ValueError):
    """A batch was refused. Nothing was written."""


def _describe(problems: Sequence[str]) -> str:
    shown = list(problems[:MAX_REPORTED_PROBLEMS])
    if len(problems) > MAX_REPORTED_PROBLEMS:
        shown.append(f"... and {len(problems) - MAX_REPORTED_PROBLEMS} more")
    return "; ".join(shown)


def _row_problems(score: CausalUpliftScore, quadrant: object) -> list[str]:
    """Everything wrong with one score, as text. Empty means it may be stored."""
    problems: list[str] = []
    where = f"risk {score.risk_id}"

    low, high, point = score.ci_low_bps, score.ci_high_bps, score.uplift_bps

    if low > high:
        problems.append(f"{where}: interval is inverted ({low} > {high})")
    elif not low <= point <= high:
        # The reason this module refuses rather than adjusts. See the module
        # docstring: the interval is reported as computed or not at all.
        problems.append(f"{where}: uplift {point} lies outside its interval [{low}, {high}]")

    if not RATE_MIN <= score.p_treat_bps <= RATE_MAX:
        problems.append(f"{where}: p_treat_bps {score.p_treat_bps} outside [0, {BPS_SCALE}]")
    if not RATE_MIN <= score.p_control_bps <= RATE_MAX:
        problems.append(f"{where}: p_control_bps {score.p_control_bps} outside [0, {BPS_SCALE}]")
    if not UPLIFT_MIN <= point <= UPLIFT_MAX:
        problems.append(f"{where}: uplift_bps {point} outside [{UPLIFT_MIN}, {UPLIFT_MAX}]")

    value = quadrant.value if isinstance(quadrant, Quadrant) else quadrant
    if value not in Quadrant.values():
        problems.append(f"{where}: {value!r} is not a quadrant")

    if not score.model_version:
        problems.append(f"{where}: model_version is blank")

    return problems


def _to_row(
    assignment: QuadrantAssignment,
    experiment_id: uuid.UUID,
    as_of: datetime,
) -> UpliftScoreRow:
    """Causal dataclass -> ORM row, field by field.

    Deliberately explicit rather than reflective: the two types share a name and
    only partly overlap, and the fields the causal layer keeps but does not
    persist — `fold`, `cell_key`, `harm_uplift_bps`, the qualification reason —
    are dropped here on purpose. `harm_uplift_bps` in particular decides a
    quadrant and is then discarded; there is no column for it, by design.
    """
    score = assignment.uplift
    return UpliftScoreRow(
        risk_id=score.risk_id,
        experiment_id=experiment_id,
        model_version=score.model_version,
        p_treat_bps=score.p_treat_bps,
        p_control_bps=score.p_control_bps,
        uplift_bps=score.uplift_bps,
        uplift_ci_low_bps=score.ci_low_bps,
        uplift_ci_high_bps=score.ci_high_bps,
        quadrant=assignment.quadrant.value,
        scored_at=as_of,
    )


def _enrolled_risk_ids(session: Session, experiment_id: uuid.UUID) -> set[uuid.UUID]:
    """Risks actually randomised into this experiment.

    `case_assignments` is the enrolment record, so it is also the authority on
    what may be scored. A score for a risk that was never assigned has no arm
    behind it and no population it belongs to.
    """
    return set(
        session.execute(
            select(CaseAssignment.risk_id).where(CaseAssignment.experiment_id == experiment_id)
        ).scalars()
    )


def existing_versions(
    session: Session,
    experiment_id: uuid.UUID,
    model_versions: Sequence[str],
) -> dict[str, int]:
    """Model versions already stored for this experiment, with row counts."""
    if not model_versions:
        return {}
    rows = session.execute(
        select(UpliftScoreRow.model_version, func.count())
        .where(
            UpliftScoreRow.experiment_id == experiment_id,
            UpliftScoreRow.model_version.in_(sorted(set(model_versions))),
        )
        .group_by(UpliftScoreRow.model_version)
    ).all()
    return {version: count for version, count in rows}


def persist_scores(
    session: Session,
    experiment_id: uuid.UUID,
    assignments: Sequence[QuadrantAssignment],
    *,
    as_of: datetime,
) -> tuple[UpliftScoreRow, ...]:
    """Store a completed scoring run. All of it, or none of it.

    `as_of` becomes `scored_at` for every row. It is injected rather than read
    from a clock so that the same population scored twice produces byte-identical
    rows, and so a backfill can state when the scoring it represents actually
    happened. There is no server-side default on the column to fall back on.

    Rows are inserted in `risk_id` order, which makes a re-run reproducible down
    to the sequence of INSERTs.

    Returns the persisted rows. Does not commit — the caller owns the
    transaction, and a repository that committed would decide on the caller's
    behalf that a partial pipeline run should survive.
    """
    if experiment_id is None:
        raise UpliftPersistenceError("experiment_id is required")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        # `scored_at` is timestamptz; a naive datetime would be silently read as
        # the server's zone and quietly shift the recorded time.
        raise UpliftPersistenceError(f"as_of must be timezone-aware, got {as_of!r}")

    if not assignments:
        return ()

    ordered = sorted(assignments, key=lambda item: item.uplift.risk_id)

    problems: list[str] = []

    seen: set[uuid.UUID] = set()
    duplicates: set[uuid.UUID] = set()
    for assignment in ordered:
        risk_id = assignment.uplift.risk_id
        if risk_id in seen:
            duplicates.add(risk_id)
        seen.add(risk_id)
    problems.extend(
        f"risk {risk_id} appears more than once in the batch" for risk_id in sorted(duplicates)
    )

    for assignment in ordered:
        problems.extend(_row_problems(assignment.uplift, assignment.quadrant))

    enrolled = _enrolled_risk_ids(session, experiment_id)
    unenrolled = sorted(risk_id for risk_id in seen if risk_id not in enrolled)
    problems.extend(
        f"risk {risk_id} is not enrolled in experiment {experiment_id}" for risk_id in unenrolled
    )

    versions = {assignment.uplift.model_version for assignment in ordered}
    already = existing_versions(session, experiment_id, sorted(versions))
    problems.extend(
        f"experiment {experiment_id} already holds {count} score(s) for model {version!r}"
        for version, count in sorted(already.items())
    )

    if problems:
        raise UpliftPersistenceError(
            f"refusing to persist {len(ordered)} score(s): {_describe(problems)}"
        )

    rows = tuple(_to_row(assignment, experiment_id, as_of) for assignment in ordered)
    for row in rows:
        session.add(row)
    session.flush()
    return rows


__all__ = [
    "BPS_SCALE",
    "MAX_REPORTED_PROBLEMS",
    "RATE_MAX",
    "RATE_MIN",
    "UPLIFT_MAX",
    "UPLIFT_MIN",
    "UpliftPersistenceError",
    "existing_versions",
    "persist_scores",
]
