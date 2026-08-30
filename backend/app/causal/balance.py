"""Covariate balance across arms, as exact integers.

Randomisation is a claim, not a guarantee. With a few hundred units a fair
draw can still hand one arm the bigger cases, and a difference in outcomes
would then be indistinguishable from a difference in inputs. The balance table
is what turns "we randomised" into something a reader can check: for each
pre-registered covariate, how far apart the arms actually landed.

The measure is the **standardised mean difference**, treatment minus holdout,
divided by the pooled within-arm standard deviation:

    smd = (mean_t - mean_h) / sqrt((var_t + var_h) / 2)

expressed in basis points, and flagged when ``|smd_bps| > 1000`` — the
basis-point form of the conventional ``|SMD| > 0.1``.

**No float is ever formed.** Both the difference and the pooled variance are
carried as exact integer ratios, and the square root is taken with
:func:`math.isqrt` against a rounding criterion that is itself an integer
comparison. A balance verdict is therefore bit-identical on any machine, and
the threshold comparison is an integer comparison — the same discipline ADR
0001 applies to money, for the same reason: a number that decides something
must be reproducible.

Sign convention is treatment minus holdout throughout, so a positive
``smd_bps`` means the treated arm ran higher on that covariate.

Denominators are the **arm sizes at randomisation**, not the number of units
with a value. A covariate that is absent for part of the population —
``payment_method`` is, definitionally, for two of the four risk types — is
reported with an explicit missing level rather than quietly dropped, because
dropping it would compute balance on a subpopulation that is itself selected.

This module reads and computes. It writes nothing at all: no recovery case, no
recovery action, no audit row. It estimates no effect, scores no uplift, and
recommends nothing — those are later components, and a diagnostic that could
also act is a diagnostic no one can trust.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaseAssignment, PaymentAttempt, RevenueRisk
from app.models.enums import Arm

#: Full basis-point scale.
BPS_SCALE = 10_000

#: ``|SMD| > 0.1`` in basis points. Strictly greater: exactly 1000 is not
#: flagged, matching the convention it encodes.
IMBALANCE_THRESHOLD_BPS = 1_000

#: Separator in `case_assignments.stratum_key`, written by assignment as
#: ``f"{risk_type}|{amount_band}"``.
STRATUM_SEPARATOR = "|"

#: Explicit level for a covariate that has no value for a unit. Sorted last so
#: the table reads with the real levels first.
MISSING_LEVEL = "(missing)"

#: The pre-registered balance covariates, in report order.
BALANCE_COVARIATES: tuple[str, ...] = (
    "risk_type",
    "amount_band",
    "amount_at_risk",
    "confidence_bps",
    "payment_method",
)

CONTINUOUS = "continuous"
CATEGORICAL = "categorical"

#: Sort floor for an attempt with no `attempted_at`. Not a clock read — a
#: fixed constant, so ordering stays deterministic.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class BalanceError(ValueError):
    """Balance could not be computed, and the caller should know why."""


# -- exact integer square root -------------------------------------------


def round_sqrt_ratio(num: int, den: int) -> int:
    """``round(sqrt(num / den))`` for non-negative integers, exactly.

    ``math.isqrt`` gives the floor; the answer is that floor or one more, and
    which one is decided by an integer comparison rather than by forming the
    root. Rounds halves up: ``sqrt`` landing exactly on ``m + 1/2`` yields
    ``m + 1``.
    """
    if den <= 0:
        raise BalanceError(f"denominator must be positive, got {den}")
    if num < 0:
        raise BalanceError(f"numerator must be non-negative, got {num}")

    floor_root = math.isqrt(num // den)
    # Round up when sqrt(num/den) >= floor_root + 1/2, i.e. when
    # 4 * num >= (2 * floor_root + 1)^2 * den. All integers.
    if (2 * floor_root + 1) ** 2 * den <= 4 * num:
        return floor_root + 1
    return floor_root


# -- the measure ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Smd:
    """One standardised mean difference, treatment minus holdout."""

    smd_bps: int | None
    treatment_n: int
    holdout_n: int
    undefined_reason: str | None = None

    @property
    def is_defined(self) -> bool:
        return self.smd_bps is not None

    @property
    def flagged(self) -> bool:
        """Whether this covariate needs a human look.

        An undefined SMD is flagged. "We could not check" must not read the
        same as "we checked and it was fine".
        """
        if self.smd_bps is None:
            return True
        return abs(self.smd_bps) > IMBALANCE_THRESHOLD_BPS


def _undefined(reason: str, treatment_n: int, holdout_n: int) -> Smd:
    return Smd(
        smd_bps=None,
        treatment_n=treatment_n,
        holdout_n=holdout_n,
        undefined_reason=reason,
    )


def _smd_from_ratios(
    diff_num: int,
    diff_den: int,
    var_num: int,
    var_den: int,
    treatment_n: int,
    holdout_n: int,
) -> Smd:
    """Assemble ``(diff_num/diff_den) / sqrt(var_num/var_den)`` in basis points.

    Squaring the whole quantity turns the division-by-a-root into a single
    rational whose root is taken once, which is what keeps every intermediate
    an exact integer.
    """
    if var_num == 0:
        if diff_num == 0:
            return Smd(smd_bps=0, treatment_n=treatment_n, holdout_n=holdout_n)
        return _undefined(
            "pooled variance is zero but the arm means differ: every unit in "
            "each arm holds the same value, so the difference cannot be standardised",
            treatment_n,
            holdout_n,
        )

    magnitude = round_sqrt_ratio(
        (BPS_SCALE * abs(diff_num)) ** 2 * var_den,
        diff_den**2 * var_num,
    )
    return Smd(
        smd_bps=-magnitude if diff_num < 0 else magnitude,
        treatment_n=treatment_n,
        holdout_n=holdout_n,
    )


def mean_smd_bps(treatment: Sequence[int], holdout: Sequence[int]) -> Smd:
    """SMD between two arms of integer values, using the sample variance.

    Everything is carried as an exact ratio:

        diff  = (S_t * n_h - S_h * n_t) / (n_t * n_h)
        var_t = (n_t * Q_t - S_t^2) / (n_t * (n_t - 1))

    with ``S`` the sum and ``Q`` the sum of squares, so no rounding happens
    before the single square root at the end.
    """
    n_t, n_h = len(treatment), len(holdout)
    if n_t < 2 or n_h < 2:
        return _undefined(
            "a variance needs at least two units in each arm",
            n_t,
            n_h,
        )
    for value in (*treatment, *holdout):
        if isinstance(value, bool) or not isinstance(value, int):
            raise BalanceError(
                f"covariate values must be integers, got {type(value).__name__}; "
                "a float here would make the verdict irreproducible"
            )

    sum_t, sum_h = sum(treatment), sum(holdout)
    sq_t = sum(value * value for value in treatment)
    sq_h = sum(value * value for value in holdout)

    diff_num = sum_t * n_h - sum_h * n_t
    diff_den = n_t * n_h

    # var_t = a_num / a_den, var_h = b_num / b_den; pooled = (var_t + var_h) / 2.
    a_num, a_den = n_t * sq_t - sum_t * sum_t, n_t * (n_t - 1)
    b_num, b_den = n_h * sq_h - sum_h * sum_h, n_h * (n_h - 1)
    var_num = a_num * b_den + b_num * a_den
    var_den = 2 * a_den * b_den

    return _smd_from_ratios(diff_num, diff_den, var_num, var_den, n_t, n_h)


def proportion_smd_bps(
    treatment_hits: int,
    treatment_n: int,
    holdout_hits: int,
    holdout_n: int,
) -> Smd:
    """SMD between two proportions, the standard binary form.

        smd = (p_t - p_h) / sqrt((p_t(1 - p_t) + p_h(1 - p_h)) / 2)

    Used for each level of a categorical covariate: a level is the indicator
    "this unit is of this level", and balance on a category means balance on
    every one of its levels.
    """
    if treatment_n < 1 or holdout_n < 1:
        return _undefined("an arm with no units has no proportion", treatment_n, holdout_n)
    if not 0 <= treatment_hits <= treatment_n:
        raise BalanceError(f"treatment_hits {treatment_hits} outside 0..{treatment_n}")
    if not 0 <= holdout_hits <= holdout_n:
        raise BalanceError(f"holdout_hits {holdout_hits} outside 0..{holdout_n}")

    diff_num = treatment_hits * holdout_n - holdout_hits * treatment_n
    diff_den = treatment_n * holdout_n

    treatment_var = treatment_hits * (treatment_n - treatment_hits) * holdout_n * holdout_n
    holdout_var = holdout_hits * (holdout_n - holdout_hits) * treatment_n * treatment_n
    var_num = treatment_var + holdout_var
    var_den = 2 * treatment_n * treatment_n * holdout_n * holdout_n

    return _smd_from_ratios(diff_num, diff_den, var_num, var_den, treatment_n, holdout_n)


# -- the unit of analysis -------------------------------------------------


def split_stratum_key(stratum_key: str) -> tuple[str, str]:
    """``"risk_type|amount_band"`` back into its two parts.

    Read from the assignment rather than recomputed, because balance is a
    check on the randomisation and the randomisation saw exactly this key.
    """
    risk_type, separator, amount_band = stratum_key.partition(STRATUM_SEPARATOR)
    if not separator or not risk_type or not amount_band:
        raise BalanceError(
            f"malformed stratum_key {stratum_key!r}: expected "
            f"'risk_type{STRATUM_SEPARATOR}amount_band'"
        )
    return risk_type, amount_band


@dataclass(frozen=True, slots=True)
class CovariateRow:
    """One randomised unit and its balance covariates.

    Deliberately narrow. Analysis sees the covariates it pre-registered and
    nothing else — no outcome, no arm-specific field, and no ground truth.
    """

    risk_id: uuid.UUID
    arm: str
    risk_type: str
    amount_band: str
    amount_at_risk: int
    confidence_bps: int
    payment_method: str | None = None

    @property
    def is_treatment(self) -> bool:
        return self.arm == Arm.TREATMENT.value


# -- per-covariate results ------------------------------------------------


@dataclass(frozen=True, slots=True)
class LevelBalance:
    """One level of a categorical covariate."""

    level: str
    treatment_count: int
    holdout_count: int
    smd: Smd

    @property
    def flagged(self) -> bool:
        return self.smd.flagged


@dataclass(frozen=True, slots=True)
class CovariateBalance:
    """Balance for one covariate: a single SMD, or one per level."""

    name: str
    kind: str
    smd: Smd | None = None
    levels: tuple[LevelBalance, ...] = ()

    @property
    def flagged(self) -> bool:
        if self.kind == CONTINUOUS:
            return self.smd is None or self.smd.flagged
        # No levels at all means nothing was checked, which must not report as
        # balanced — the same rule the undefined SMD follows.
        return not self.levels or any(level.flagged for level in self.levels)

    @property
    def flagged_levels(self) -> tuple[str, ...]:
        return tuple(level.level for level in self.levels if level.flagged)

    @property
    def worst_smd_bps(self) -> int | None:
        """Largest ``|smd_bps|`` here, or None if any of them is undefined."""
        if self.kind == CONTINUOUS:
            return self.smd.smd_bps if self.smd is not None else None
        values = [level.smd.smd_bps for level in self.levels]
        if not values or any(value is None for value in values):
            return None
        return max((v for v in values if v is not None), key=abs)


def continuous_balance(
    name: str,
    rows: Sequence[CovariateRow],
    value: Callable[[CovariateRow], int],
) -> CovariateBalance:
    """Balance on an integer-valued covariate."""
    treatment = [value(row) for row in rows if row.is_treatment]
    holdout = [value(row) for row in rows if not row.is_treatment]
    return CovariateBalance(
        name=name,
        kind=CONTINUOUS,
        smd=mean_smd_bps(treatment, holdout),
    )


def _level_sort_key(level: str) -> tuple[int, str]:
    return (1, level) if level == MISSING_LEVEL else (0, level)


def categorical_balance(
    name: str,
    rows: Sequence[CovariateRow],
    value: Callable[[CovariateRow], str | None],
) -> CovariateBalance:
    """Balance on a categorical covariate, one SMD per observed level.

    A ``None`` becomes the explicit missing level, counted against the full arm
    denominator. Reporting the gap is the point: a covariate that is absent
    unevenly across arms is itself an imbalance.
    """
    treatment_n = sum(1 for row in rows if row.is_treatment)
    holdout_n = len(rows) - treatment_n

    counts: dict[str, list[int]] = {}
    for row in rows:
        level = value(row) or MISSING_LEVEL
        bucket = counts.setdefault(level, [0, 0])
        bucket[0 if row.is_treatment else 1] += 1

    levels = tuple(
        LevelBalance(
            level=level,
            treatment_count=counts[level][0],
            holdout_count=counts[level][1],
            smd=proportion_smd_bps(counts[level][0], treatment_n, counts[level][1], holdout_n),
        )
        for level in sorted(counts, key=_level_sort_key)
    )
    return CovariateBalance(name=name, kind=CATEGORICAL, levels=levels)


# -- the report -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BalanceReport:
    """The balance table for one experiment."""

    experiment_id: uuid.UUID | None
    treatment_n: int
    holdout_n: int
    covariates: tuple[CovariateBalance, ...]

    @property
    def total_n(self) -> int:
        return self.treatment_n + self.holdout_n

    @property
    def flagged(self) -> tuple[str, ...]:
        """Covariate names needing a look, in report order."""
        return tuple(covariate.name for covariate in self.covariates if covariate.flagged)

    @property
    def is_balanced(self) -> bool:
        return not self.flagged

    def as_dict(self) -> dict[str, object]:
        """A plain-data view, for a report or a snapshot. Integers only."""

        def covariate_dict(covariate: CovariateBalance) -> dict[str, object]:
            smd = covariate.smd
            return {
                "name": covariate.name,
                "kind": covariate.kind,
                "smd_bps": smd.smd_bps if smd else None,
                "undefined_reason": smd.undefined_reason if smd else None,
                "levels": [
                    {
                        "level": level.level,
                        "treatment_count": level.treatment_count,
                        "holdout_count": level.holdout_count,
                        "smd_bps": level.smd.smd_bps,
                        "undefined_reason": level.smd.undefined_reason,
                    }
                    for level in covariate.levels
                ],
            }

        return {
            "experiment_id": str(self.experiment_id) if self.experiment_id else None,
            "treatment_n": self.treatment_n,
            "holdout_n": self.holdout_n,
            "threshold_bps": IMBALANCE_THRESHOLD_BPS,
            "is_balanced": self.is_balanced,
            "flagged": list(self.flagged),
            "covariates": [covariate_dict(covariate) for covariate in self.covariates],
        }


def balance_report(
    rows: Sequence[CovariateRow],
    experiment_id: uuid.UUID | None = None,
) -> BalanceReport:
    """The full balance table over already-loaded rows. Pure.

    Covariates appear in the pre-registered order so two runs produce the same
    table, and so a reader compares like with like across experiments.
    """
    treatment_n = sum(1 for row in rows if row.is_treatment)
    holdout_n = len(rows) - treatment_n

    covariates = (
        categorical_balance("risk_type", rows, lambda row: row.risk_type),
        categorical_balance("amount_band", rows, lambda row: row.amount_band),
        continuous_balance("amount_at_risk", rows, lambda row: row.amount_at_risk),
        continuous_balance("confidence_bps", rows, lambda row: row.confidence_bps),
        categorical_balance("payment_method", rows, lambda row: row.payment_method),
    )
    return BalanceReport(
        experiment_id=experiment_id,
        treatment_n=treatment_n,
        holdout_n=holdout_n,
        covariates=covariates,
    )


# -- loading --------------------------------------------------------------


def _attempt_rank(attempt: PaymentAttempt) -> tuple[int, int, datetime, str]:
    """Deterministic ordering, latest attempt last.

    Ordered on stored values only — never on row order — so the method a report
    picks does not depend on how the rows came back. An unstamped attempt sorts
    below a stamped one rather than being compared through a float epoch.
    """
    attempted_at = attempt.attempted_at
    return (
        attempt.attempt_number,
        0 if attempted_at is None else 1,
        _EPOCH if attempted_at is None else attempted_at,
        str(attempt.id),
    )


def _payment_methods(session: Session, order_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Latest payment method per order, for the orders that have one."""
    wanted = {order_id for order_id in order_ids if order_id is not None}
    if not wanted:
        return {}

    attempts = session.execute(
        select(PaymentAttempt).where(PaymentAttempt.order_id.in_(wanted))
    ).scalars()

    latest: dict[uuid.UUID, PaymentAttempt] = {}
    for attempt in attempts:
        held = latest.get(attempt.order_id)
        if held is None or _attempt_rank(attempt) > _attempt_rank(held):
            latest[attempt.order_id] = attempt
    return {order_id: attempt.payment_method for order_id, attempt in latest.items()}


def covariate_rows(session: Session, experiment_id: uuid.UUID) -> list[CovariateRow]:
    """Load the randomised population of one experiment.

    The denominator is fixed at randomisation: every assignment is loaded,
    whether or not a window opened, whether or not execution succeeded, and
    whether or not an outcome exists. Filtering any of those out would replace
    the randomised population with a selected one, which is exactly the bias
    the holdout exists to remove.

    ``amount_at_risk`` and ``confidence_bps`` are read live from the risk;
    ``risk_type`` and ``amount_band`` come from the stored ``stratum_key``, the
    values randomisation actually used.
    """
    statement = (
        select(CaseAssignment, RevenueRisk)
        .join(RevenueRisk, RevenueRisk.id == CaseAssignment.risk_id)
        .where(CaseAssignment.experiment_id == experiment_id)
        .order_by(CaseAssignment.risk_id)
    )
    pairs = list(session.execute(statement).all())
    methods = _payment_methods(session, (risk.order_id for _, risk in pairs))

    rows: list[CovariateRow] = []
    for assignment, risk in pairs:
        risk_type, band = split_stratum_key(assignment.stratum_key)
        rows.append(
            CovariateRow(
                risk_id=risk.id,
                arm=assignment.arm,
                risk_type=risk_type,
                amount_band=band,
                amount_at_risk=risk.amount_at_risk,
                confidence_bps=risk.confidence_bps,
                payment_method=methods.get(risk.order_id) if risk.order_id else None,
            )
        )
    return rows


def report_for_experiment(session: Session, experiment_id: uuid.UUID) -> BalanceReport:
    """Load and report. Reads only; writes nothing."""
    return balance_report(covariate_rows(session, experiment_id), experiment_id)


__all__ = [
    "BALANCE_COVARIATES",
    "BPS_SCALE",
    "CATEGORICAL",
    "CONTINUOUS",
    "IMBALANCE_THRESHOLD_BPS",
    "MISSING_LEVEL",
    "STRATUM_SEPARATOR",
    "BalanceError",
    "BalanceReport",
    "CovariateBalance",
    "CovariateRow",
    "LevelBalance",
    "Smd",
    "balance_report",
    "categorical_balance",
    "continuous_balance",
    "covariate_rows",
    "mean_smd_bps",
    "proportion_smd_bps",
    "report_for_experiment",
    "round_sqrt_ratio",
    "split_stratum_key",
]
