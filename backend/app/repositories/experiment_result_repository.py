"""Experiment-result persistence.

`experiment_results` is the **only** table this module writes to. It cannot
reach `uplift_scores`, `recovery_cases`, `recovery_actions` or `audit_events`:
storing a measurement is not a decision, and certainly not an approval.

**Nothing here computes anything.** Every value arrives already calculated by
the Day 5 reporting layer, in integer arithmetic, and this module's job is to
check it against the column constraints and hand it to the database. There is no
ATE, no interval, no Qini coefficient, no ledger amount and no harm figure
derived here. A second implementation in the repository would be unverified by
construction, and would silently diverge the moment either side changed.

The input is `ResultValues` — fifteen plain scalars — rather than the reporting
layer's own `EvaluationReport`. That keeps `app/repositories/` free of any
import of `app.reporting.evaluation`, which a Phase 3 isolation guard forbids
across the whole of `app/`; see `ResultValues` for why the caller does the
mapping. It also makes the no-recomputation property structural: there is no
estimator reachable from this module.

**Four columns are stored as NULL, deliberately.**

* `qini_coefficient_bps` is NULL when Q(N) is zero — there was no incremental
  recovery to apportion, so the coefficient is undefined. That is a different
  claim from zero, which says the ranking did no better than chance. `None` is
  carried through exactly as the report produced it and is never coerced.
* `harm_cost`, `action_cost` and `net_incremental_value` are **always** NULL
  today, and this module offers no way to set them. `harm_cost` needs a monetary
  value per harm event, which the schema records three kinds of and values none
  of; `action_cost` needs executed actions, and `recovery_actions` is empty until
  Phase 6+; `net_incremental_value` needs a gross margin, which exists nowhere in
  this codebase. They are not parameters because a parameter is an invitation to
  pass a zero, and a zero in a money column is a measurement nobody made.

  When an economic basis exists, they become arguments in a deliberate change —
  along with whatever records where the assumption came from.

The same guards exist in the database as CHECKs. Both are kept: the CHECK holds
against a psql session and a future migration, and the guard here turns a
mid-flush `IntegrityError` naming one column into an error naming every problem
in the row.

**Append-only.** The table stores one row per *computation*, stamped
`computed_at`, so an interim reading and a final reading both survive and can be
told apart — the difference between them is exactly what peeking would hide. The
schema therefore has no unique constraint, and repeated computation is expected.
The pre-insert duplicate check is **sequential only**: it looks for a row with
the same `(experiment_id, computed_at)`, and a concurrent transaction's
uncommitted row is invisible to this session's SELECT, so two simultaneous
writers could both pass it. Unlike `uplift_scores` there is no unique index
behind it, so nothing catches that case at the database. Result computation is
single-process today, the same limitation `risk_repository` documents for
detection.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Experiment, ExperimentResult
from app.models.experiment_result import P_VALUE_SCALE

#: Basis-point bounds, mirroring the table's CHECK constraints.
BPS_SCALE = 10_000
RATE_MIN, RATE_MAX = 0, BPS_SCALE
EFFECT_MIN, EFFECT_MAX = -BPS_SCALE, BPS_SCALE

#: How many problems an error message lists before summarising.
MAX_REPORTED_PROBLEMS = 5


class ExperimentResultPersistenceError(ValueError):
    """A result was refused. Nothing was written."""


@dataclass(frozen=True, slots=True)
class ResultValues:
    """The fifteen already-computed numbers this table stores.

    Flat and scalar on purpose. The repository used to take the Day 5
    `EvaluationReport` directly, which read better but made
    `app/repositories/` import `app.reporting.evaluation` — and a Phase 3
    isolation guard forbids **any** module under `app/` from importing a module
    whose name contains "evaluation", because that is how the detection answer
    key would leak out of the test harness. The guard is coarser than its stated
    purpose, but it is load-bearing and not this module's to relax.

    So the reporting layer stays on the far side of the boundary and the caller
    does the mapping. That has a second benefit worth keeping: every value here
    is a plain integer or bool, so there is visibly nothing for this module to
    recompute. It cannot reach an estimator even by accident.

    `qini_coefficient_bps` is `int | None`, and `None` means undefined — Q(N) was
    zero, so there was no incremental recovery to apportion. It is never a zero.

    `harm_cost`, `action_cost` and `net_incremental_value` are deliberately
    **absent** from this type. They have no honest source, and a field is an
    invitation to fill it.
    """

    experiment_id: uuid.UUID

    n_treat: int
    n_control: int
    rate_treat_bps: int
    rate_control_bps: int

    ate_bps: int
    ate_ci_low_bps: int
    ate_ci_high_bps: int
    p_value_micros: int

    gross_recovered: int
    incremental_recovered: int
    credited_not_earned: int

    harm_ate_bps: int
    qini_coefficient_bps: int | None
    is_underpowered: bool


def _describe(problems: Sequence[str]) -> str:
    shown = list(problems[:MAX_REPORTED_PROBLEMS])
    if len(problems) > MAX_REPORTED_PROBLEMS:
        shown.append(f"... and {len(problems) - MAX_REPORTED_PROBLEMS} more")
    return "; ".join(shown)


def _problems(values: ResultValues) -> list[str]:
    """Everything wrong with these values as a row. Empty means storable.

    Mirrors every CHECK on `experiment_results`, so a violation is reported as a
    named problem rather than surfacing as an integrity error mid-flush.
    """
    problems: list[str] = []

    if values.n_treat < 0 or values.n_control < 0:
        problems.append(
            f"arm sizes must be non-negative, got {values.n_treat} treated "
            f"and {values.n_control} held out"
        )

    if not RATE_MIN <= values.rate_treat_bps <= RATE_MAX:
        problems.append(f"rate_treat_bps {values.rate_treat_bps} outside [0, {BPS_SCALE}]")
    if not RATE_MIN <= values.rate_control_bps <= RATE_MAX:
        problems.append(f"rate_control_bps {values.rate_control_bps} outside [0, {BPS_SCALE}]")

    low, high, ate = values.ate_ci_low_bps, values.ate_ci_high_bps, values.ate_bps
    if low > high:
        problems.append(f"interval is inverted ({low} > {high})")
    elif not low <= ate <= high:
        # The estimator's interval is reported as computed or not at all; it is
        # never widened to admit its own estimate.
        problems.append(f"ate_bps {ate} lies outside its interval [{low}, {high}]")
    if not EFFECT_MIN <= ate <= EFFECT_MAX:
        problems.append(f"ate_bps {ate} outside [{EFFECT_MIN}, {EFFECT_MAX}]")

    if not 0 <= values.p_value_micros <= P_VALUE_SCALE:
        problems.append(f"p_value_micros {values.p_value_micros} outside [0, {P_VALUE_SCALE}]")

    if values.gross_recovered < 0:
        problems.append(f"gross_recovered {values.gross_recovered} is negative")

    if not EFFECT_MIN <= values.harm_ate_bps <= EFFECT_MAX:
        problems.append(f"harm_ate_bps {values.harm_ate_bps} outside [{EFFECT_MIN}, {EFFECT_MAX}]")

    qini = values.qini_coefficient_bps
    if qini is not None and not EFFECT_MIN <= qini <= EFFECT_MAX:
        problems.append(f"qini_coefficient_bps {qini} outside [{EFFECT_MIN}, {EFFECT_MAX}]")

    return problems


def _to_row(
    values: ResultValues,
    experiment_id: uuid.UUID,
    as_of: datetime,
) -> ExperimentResult:
    """Values -> ORM row, field by field.

    Deliberately explicit rather than reflective: listing the correspondence is
    what makes it reviewable. `qini_coefficient_bps` is passed straight through,
    so a `None` stays a `None`. The three economic columns are set here and are
    not fields on the input.
    """
    return ExperimentResult(
        experiment_id=experiment_id,
        computed_at=as_of,
        n_treat=values.n_treat,
        n_control=values.n_control,
        rate_treat_bps=values.rate_treat_bps,
        rate_control_bps=values.rate_control_bps,
        ate_bps=values.ate_bps,
        ate_ci_low_bps=values.ate_ci_low_bps,
        ate_ci_high_bps=values.ate_ci_high_bps,
        p_value_micros=values.p_value_micros,
        gross_recovered=values.gross_recovered,
        incremental_recovered=values.incremental_recovered,
        credited_not_earned=values.credited_not_earned,
        harm_ate_bps=values.harm_ate_bps,
        qini_coefficient_bps=values.qini_coefficient_bps,
        is_underpowered=values.is_underpowered,
        # No economic basis exists. NULL, never zero. See the module docstring.
        harm_cost=None,
        action_cost=None,
        net_incremental_value=None,
    )


def results_at(
    session: Session,
    experiment_id: uuid.UUID,
    computed_at: datetime,
) -> int:
    """How many results this experiment already has at this instant."""
    return len(
        list(
            session.execute(
                select(ExperimentResult.id).where(
                    ExperimentResult.experiment_id == experiment_id,
                    ExperimentResult.computed_at == computed_at,
                )
            ).scalars()
        )
    )


def persist_result(
    session: Session,
    experiment_id: uuid.UUID,
    values: ResultValues,
    *,
    as_of: datetime,
) -> ExperimentResult:
    """Store one computation of one experiment's result.

    `as_of` becomes `computed_at`. It is injected rather than read from a clock
    so that the same values stored twice produce identical rows, and so a
    backfill can state when the computation it represents actually happened.
    There is no server-side default on the column to fall back on.

    Returns the persisted row. Does not commit — the caller owns the
    transaction, and a repository that committed would decide on the caller's
    behalf that a partial pipeline run should survive.
    """
    if experiment_id is None:
        raise ExperimentResultPersistenceError("experiment_id is required")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        # `computed_at` is timestamptz; a naive datetime would be silently read
        # as the server's zone and quietly shift the recorded time.
        raise ExperimentResultPersistenceError(f"as_of must be timezone-aware, got {as_of!r}")

    problems: list[str] = []

    if values.experiment_id != experiment_id:
        problems.append(
            f"values are for experiment {values.experiment_id}, not {experiment_id}; "
            "storing them here would attribute a measurement to the wrong design"
        )

    if session.get(Experiment, experiment_id) is None:
        problems.append(f"no experiment {experiment_id}")

    problems.extend(_problems(values))

    existing = results_at(session, experiment_id, as_of)
    if existing:
        problems.append(
            f"experiment {experiment_id} already has {existing} result(s) computed at "
            f"{as_of.isoformat()}; the table is append-only per computation, and two "
            "rows at the same instant would be indistinguishable"
        )

    if problems:
        raise ExperimentResultPersistenceError(
            f"refusing to persist a result for experiment {experiment_id}: {_describe(problems)}"
        )

    row = _to_row(values, experiment_id, as_of)
    session.add(row)
    session.flush()
    return row


__all__ = [
    "BPS_SCALE",
    "EFFECT_MAX",
    "EFFECT_MIN",
    "MAX_REPORTED_PROBLEMS",
    "RATE_MAX",
    "RATE_MIN",
    "ExperimentResultPersistenceError",
    "ResultValues",
    "persist_result",
    "results_at",
]
