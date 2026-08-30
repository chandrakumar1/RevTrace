"""Feature cells, folds, and the rule that decides when a cell may be labelled.

This is the substrate the uplift model sits on, and deliberately nothing more.
It builds cells, splits folds, counts arms, and answers one question:

    **is this cell large enough that a label from it means anything?**

That question is the difference between a causal system and a confident guess.
Two of the five quadrants — Sure Thing and Lost Cause — fire on a *null* result,
a confidence interval that contains zero. An underpowered cell's interval always
contains zero, so without a floor those two labels would be handed out by
default to every thin cell, and the thinner the cell the more confident the
system would look. The floor is what stops that.

**The floor is the pre-registered power calculation**, applied to the cell:

    n_holdout >= required_n_holdout(p_control_cell, mde_bps, ratio)

with `ratio` the cell's own arm split. The unequal-allocation form rather than
the balanced one, because randomisation makes cells *near* balanced, not
balanced, and the balanced form is not conservative when the arms differ. A cell
with 150 treated and 375 held-out units at a 35% control rate clears the
balanced requirement of 373 and fails the exact requirement of 665 — the
treated arm is the thin one, and the balanced form cannot see that. Checking the
holdout arm at the true ratio implies the treated arm automatically.

Two degenerate cases are refused before the formula is reached, because the
formula is undefined for both:

* an arm with **no units** — nothing to compare;
* a control rate leaving **no room for the effect** (`p_control + mde > 100%`),
  where the pre-registered effect is arithmetically impossible and no sample
  size would demonstrate it.

A cell that fails at every level of the ladder yields no label at all. Mapping
that to `GRAY_ZONE` is the quadrant layer's job; this module reports only that
nothing qualified, and why.

Nothing here reads an outcome's ground truth, and nothing here writes.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.causal.power import PowerError, required_n_holdout
from app.experiments.assignment import amount_band
from app.models import Customer, Order, PaymentAttempt, RevenueRisk
from app.models.enums import Arm, PaymentStatus

#: Full basis-point scale.
BPS_SCALE = 10_000

#: Folds for cross-fitting, as approved.
DEFAULT_FOLD_COUNT = 5

#: Salt for the fold hash. Distinct from the assignment salt so fold membership
#: and arm are independent draws rather than two views of one hash.
FOLD_SALT = "revtrace-fold-v1"

#: Buckets for features that are carried but not used as cell keys at the
#: benchmark's size. Named and tested so a larger run can promote them into the
#: ladder without inventing a representation at that point.
HOUR_BUCKETS: tuple[tuple[str, int], ...] = (
    ("night", 6),
    ("morning", 12),
    ("afternoon", 18),
)
LATE_HOUR_BUCKET = "evening"

TENURE_BUCKETS: tuple[tuple[str, int], ...] = (
    ("new", 90),
    ("established", 365),
    ("loyal", 1_095),
)
LONG_TENURE_BUCKET = "veteran"

#: Days the generator treats as the salary window. Carried as a feature because
#: it is the mechanism behind one planted stratum.
SALARY_WINDOW_DAYS: frozenset[int] = frozenset({25, 26, 27, 28, 29, 30, 1, 2, 3, 4, 5})


class CellError(ValueError):
    """A cell could not be built or judged."""


# -- features -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Features:
    """What a model is allowed to see about one unit.

    Five observable fields. `lifetime_value` is deliberately absent: the column
    exists because the schema requires it, the generator models no such
    quantity, and a placeholder zero is not a measurement.
    """

    failure_code: str
    payment_method: str
    amount_band: str
    hour_bucket: str
    tenure_bucket: str
    salary_window: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "failure_code": self.failure_code,
            "payment_method": self.payment_method,
            "amount_band": self.amount_band,
            "hour_bucket": self.hour_bucket,
            "tenure_bucket": self.tenure_bucket,
            "salary_window": self.salary_window,
        }


def _bucket(value: int, buckets: Sequence[tuple[str, int]], overflow: str) -> str:
    for label, upper_exclusive in buckets:
        if value < upper_exclusive:
            return label
    return overflow


def hour_bucket(hour: int) -> str:
    if not 0 <= hour <= 23:
        raise CellError(f"hour must be within 0..23, got {hour}")
    return _bucket(hour, HOUR_BUCKETS, LATE_HOUR_BUCKET)


def tenure_bucket(days: int) -> str:
    if days < 0:
        raise CellError(f"tenure must be non-negative, got {days}")
    return _bucket(days, TENURE_BUCKETS, LONG_TENURE_BUCKET)


# -- cell keys ------------------------------------------------------------

#: The ladder, finest first. A unit is scored at the first level whose cell
#: qualifies; the coarse level exists so a thin fine cell backs off rather than
#: losing its label entirely.
FINE = "failure_code|payment_method"
COARSE = "failure_code"


def fine_key(features: Features) -> str:
    return f"{features.failure_code}|{features.payment_method}"


def coarse_key(features: Features) -> str:
    return features.failure_code


#: Level 0 is finest. `amount_band`, `hour_bucket` and `tenure_bucket` are not
#: ladder keys: at the benchmark's size a three-way cell holds roughly fifty
#: units per arm against a floor near four hundred, so every such cell would
#: fail and every unit would end up unlabelled.
LADDER: tuple[tuple[str, Callable[[Features], str]], ...] = (
    (FINE, fine_key),
    (COARSE, coarse_key),
)


# -- folds ----------------------------------------------------------------


def fold_of(
    risk_id: uuid.UUID,
    experiment_id: uuid.UUID,
    *,
    folds: int = DEFAULT_FOLD_COUNT,
    salt: str = FOLD_SALT,
) -> int:
    """Which fold a unit is held out in. Deterministic, never drawn.

    Same construction as assignment, under a different salt: reproducible from
    stored inputs, so a rerun cross-fits identically and an auditor can check
    the split rather than trust it.
    """
    if folds < 2:
        raise CellError(f"cross-fitting needs at least two folds, got {folds}")
    digest = hashlib.sha256(f"{risk_id}:{experiment_id}:{salt}".encode()).hexdigest()
    return int(digest[:8], 16) % folds


# -- counts and qualification ---------------------------------------------


@dataclass(frozen=True, slots=True)
class CellCounts:
    """Arm sizes and recovery counts within one cell of the training folds."""

    n_treated: int = 0
    n_holdout: int = 0
    recovered_treated: int = 0
    recovered_holdout: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("n_treated", self.n_treated),
            ("n_holdout", self.n_holdout),
            ("recovered_treated", self.recovered_treated),
            ("recovered_holdout", self.recovered_holdout),
        ):
            if value < 0:
                raise CellError(f"{name} must be non-negative, got {value}")
        if self.recovered_treated > self.n_treated:
            raise CellError("recoveries cannot exceed the treated arm")
        if self.recovered_holdout > self.n_holdout:
            raise CellError("recoveries cannot exceed the holdout arm")

    @property
    def total(self) -> int:
        return self.n_treated + self.n_holdout

    @property
    def p_treat_bps(self) -> int:
        if self.n_treated == 0:
            return 0
        return (self.recovered_treated * 2 * BPS_SCALE + self.n_treated) // (2 * self.n_treated)

    @property
    def p_control_bps(self) -> int:
        if self.n_holdout == 0:
            return 0
        return (self.recovered_holdout * 2 * BPS_SCALE + self.n_holdout) // (2 * self.n_holdout)

    @property
    def holdout_ratio_bps(self) -> int:
        """The cell's own arm split, for the unequal-allocation requirement."""
        if self.total == 0:
            return 0
        return self.n_holdout * BPS_SCALE // self.total


#: Why a cell was refused. Reported so a Gray Zone count can be explained
#: rather than merely stated.
EMPTY_ARM = "empty_arm"
NO_ROOM_FOR_EFFECT = "no_room_for_effect"
DEGENERATE_RATIO = "degenerate_ratio"
UNDERPOWERED = "underpowered"
QUALIFIED = "qualified"


def qualification_reason(counts: CellCounts, *, mde_bps: int) -> str:
    """Whether the cell may carry a label, and if not, why not."""
    if counts.n_treated == 0 or counts.n_holdout == 0:
        return EMPTY_ARM

    p_control = counts.p_control_bps
    if p_control + mde_bps > BPS_SCALE:
        # The pre-registered effect would push the rate past 100%. No sample
        # size demonstrates an arithmetically impossible effect.
        return NO_ROOM_FOR_EFFECT

    ratio = counts.holdout_ratio_bps
    if not 0 < ratio < BPS_SCALE:
        # One arm rounded away entirely against the other.
        return DEGENERATE_RATIO

    try:
        required = required_n_holdout(p_control, mde_bps, holdout_bps=ratio)
    except PowerError as error:  # pragma: no cover - guarded above
        raise CellError(f"cell requirement could not be computed: {error}") from error

    return QUALIFIED if counts.n_holdout >= required else UNDERPOWERED


def qualifies(counts: CellCounts, *, mde_bps: int) -> bool:
    """Whether a label from this cell would mean anything."""
    return qualification_reason(counts, mde_bps=mde_bps) == QUALIFIED


def required_for(counts: CellCounts, *, mde_bps: int) -> int | None:
    """The holdout units this cell would need, or None when undefined."""
    if counts.n_treated == 0 or counts.n_holdout == 0:
        return None
    if counts.p_control_bps + mde_bps > BPS_SCALE:
        return None
    ratio = counts.holdout_ratio_bps
    if not 0 < ratio < BPS_SCALE:
        return None
    return required_n_holdout(counts.p_control_bps, mde_bps, holdout_bps=ratio)


# -- tallying and resolution ----------------------------------------------


@dataclass(frozen=True, slots=True)
class Unit:
    """One randomised unit, as the cell layer needs it."""

    risk_id: uuid.UUID
    arm: str
    recovered: bool
    harmed: bool
    features: Features

    @property
    def is_treatment(self) -> bool:
        return self.arm == Arm.TREATMENT.value


def tally(units: Iterable[Unit], key: Callable[[Features], str]) -> dict[str, CellCounts]:
    """Arm counts per cell over the units given. Training folds only."""
    running: dict[str, list[int]] = {}
    for unit in units:
        bucket = running.setdefault(key(unit.features), [0, 0, 0, 0])
        if unit.is_treatment:
            bucket[0] += 1
            bucket[1] += int(unit.recovered)
        else:
            bucket[2] += 1
            bucket[3] += int(unit.recovered)

    return {
        cell: CellCounts(
            n_treated=values[0],
            recovered_treated=values[1],
            n_holdout=values[2],
            recovered_holdout=values[3],
        )
        for cell, values in running.items()
    }


@dataclass(frozen=True, slots=True)
class CellResolution:
    """Which cell scores a unit, or that none does.

    `level is None` means no level qualified. The quadrant layer maps that to
    GRAY_ZONE; this module does not name quadrants, so the two concerns stay
    separable and testable apart.
    """

    risk_id: uuid.UUID
    level: int | None
    level_name: str | None
    key: str | None
    counts: CellCounts | None
    reason: str

    @property
    def is_gray_zone(self) -> bool:
        return self.level is None


def resolve(
    unit: Unit,
    tallies: Sequence[Mapping[str, CellCounts]],
    *,
    mde_bps: int,
) -> CellResolution:
    """Walk the ladder and return the first qualifying cell.

    `tallies` is one mapping per ladder level, finest first, each built from the
    **training folds only**. Nothing about the unit being scored contributes to
    the counts that decide its own cell.
    """
    if len(tallies) != len(LADDER):
        raise CellError(f"expected {len(LADDER)} tallies, got {len(tallies)}")

    last_reason = EMPTY_ARM
    for level, ((name, key), counts_by_cell) in enumerate(zip(LADDER, tallies, strict=True)):
        cell = key(unit.features)
        counts = counts_by_cell.get(cell)
        if counts is None:
            last_reason = EMPTY_ARM
            continue

        reason = qualification_reason(counts, mde_bps=mde_bps)
        if reason == QUALIFIED:
            return CellResolution(
                risk_id=unit.risk_id,
                level=level,
                level_name=name,
                key=cell,
                counts=counts,
                reason=QUALIFIED,
            )
        last_reason = reason

    return CellResolution(
        risk_id=unit.risk_id,
        level=None,
        level_name=None,
        key=None,
        counts=None,
        reason=last_reason,
    )


# -- loading --------------------------------------------------------------


def load_features(session: Session, experiment_id: uuid.UUID) -> dict[uuid.UUID, Features]:
    """Observable features per enrolled risk, keyed by `risk_id`.

    Reads the first failed attempt for the risk's order, which is where a
    payment processor's `failure_code` and method live, and the customer's
    `created_at` for tenure. Reads no outcome and no truth column: the arm and
    the recovery come from `analysis.py`, which is left untouched.
    """
    from app.models import CaseAssignment

    statement = (
        select(RevenueRisk, PaymentAttempt, Customer)
        .join(CaseAssignment, CaseAssignment.risk_id == RevenueRisk.id)
        .join(Order, Order.id == RevenueRisk.order_id)
        .join(PaymentAttempt, PaymentAttempt.order_id == Order.id)
        .join(Customer, Customer.id == RevenueRisk.customer_id)
        .where(
            CaseAssignment.experiment_id == experiment_id,
            PaymentAttempt.status == PaymentStatus.FAILED.value,
            PaymentAttempt.attempt_number == 1,
        )
        .order_by(RevenueRisk.id)
    )

    features: dict[uuid.UUID, Features] = {}
    for risk, attempt, customer in session.execute(statement).all():
        detected = risk.detected_at
        features[risk.id] = Features(
            failure_code=attempt.failure_code or "unknown",
            payment_method=attempt.payment_method,
            amount_band=amount_band(risk.amount_at_risk),
            hour_bucket=hour_bucket(detected.hour),
            tenure_bucket=tenure_bucket(max(0, (detected - customer.created_at).days)),
            salary_window=detected.day in SALARY_WINDOW_DAYS,
        )
    return features


__all__ = [
    "BPS_SCALE",
    "COARSE",
    "DEGENERATE_RATIO",
    "DEFAULT_FOLD_COUNT",
    "EMPTY_ARM",
    "FINE",
    "FOLD_SALT",
    "HOUR_BUCKETS",
    "LADDER",
    "NO_ROOM_FOR_EFFECT",
    "QUALIFIED",
    "SALARY_WINDOW_DAYS",
    "TENURE_BUCKETS",
    "UNDERPOWERED",
    "CellCounts",
    "CellError",
    "CellResolution",
    "Features",
    "Unit",
    "coarse_key",
    "fine_key",
    "fold_of",
    "hour_bucket",
    "load_features",
    "qualification_reason",
    "qualifies",
    "required_for",
    "resolve",
    "tally",
    "tenure_bucket",
]
