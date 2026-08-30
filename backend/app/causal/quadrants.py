"""Turning an uplift estimate into a decision the policy gate can act on.

Five labels, and only one of them means "act":

    PERSUADABLE   the effect is real and there is room for it
    SURE_THING    they were going to pay anyway; acting buys nothing
    LOST_CAUSE    they were never going to pay; acting buys nothing
    SLEEPING_DOG  acting makes things worse
    GRAY_ZONE     we do not know, and saying so is the honest answer

Two of these — Sure Thing and Lost Cause — fire on a confidence interval that
*contains* zero. That is a null result, and a null result from too little data
is not a finding, it is an absence of one. So the first rule is the minimum-cell
rule: a unit whose cell never qualified is Gray Zone before any other rule is
consulted. Without that ordering the system would grow more confident exactly
where it knew least.

**Thresholds are fold-local.** Each fold derives its self-recovery ceiling, its
control-rate tertiles and its harm threshold from the four folds it trained on,
and applies them only to the fold it held out. Deriving them from the whole
population first would let a unit's own outcome move the boundary it is then
judged against — a small leak, but the kind that makes a cross-fitted number
quietly stop meaning what it says.

**Harm lives in memory.** The harm uplift decides a label and is then discarded;
only the label is ever persisted. There is no schema column for it and this
module does not ask for one.

The confusion matrix against the planted strata is deliberately **not** here.
Comparing a label to the answer key is the evaluation reporter's job, and it is
the only part of the application permitted to read it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.causal.cells import Unit, fold_of
from app.causal.estimators import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, bootstrap_interval
from app.causal.uplift import (
    DEFAULT_FOLD_COUNT,
    FoldModel,
    UpliftScore,
    fit_fold,
    score,
    uplift_statistic,
)
from app.models.enums import Quadrant

#: Full basis-point scale.
BPS_SCALE = 10_000

#: Which rule fired, recorded alongside the label so a classification can be
#: explained rather than merely stated.
RULE_NOT_QUALIFYING = "not_qualifying"
RULE_NEGATIVE_UPLIFT = "negative_uplift"
RULE_HARMFUL = "harm_above_threshold"
RULE_SIGNIFICANT_UPLIFT = "significant_uplift_below_ceiling"
RULE_HIGH_BASELINE = "null_effect_high_baseline"
RULE_LOW_BASELINE = "null_effect_low_baseline"
RULE_UNDECIDED = "undecided"


class QuadrantError(ValueError):
    """A classification could not be made."""


def _bernoulli_arm(hits: int, total: int) -> list[int]:
    """A 0/1 arm from its counts, for the bootstrap."""
    if not 0 <= hits <= total:
        raise QuadrantError(f"cannot build an arm of {total} from {hits} hits")
    return [1] * hits + [0] * (total - hits)


# -- fold-local thresholds ------------------------------------------------


@dataclass(frozen=True, slots=True)
class FoldThresholds:
    """The four numbers one fold judges its held-out units against.

    Every one is derived from that fold's *training* data. They are reported
    per fold rather than averaged, because an average would hide a fold whose
    boundaries sat somewhere unusual.
    """

    fold: int
    self_recovery_ceiling_bps: int
    low_tertile_bps: int
    high_tertile_bps: int
    harm_threshold_bps: int
    training_size: int
    qualifying_units: int

    def as_dict(self) -> dict[str, object]:
        return {
            "fold": self.fold,
            "self_recovery_ceiling_bps": self.self_recovery_ceiling_bps,
            "low_tertile_bps": self.low_tertile_bps,
            "high_tertile_bps": self.high_tertile_bps,
            "harm_threshold_bps": self.harm_threshold_bps,
            "training_size": self.training_size,
            "qualifying_units": self.qualifying_units,
        }


def _tertiles(values: Sequence[int]) -> tuple[int, int]:
    """Lower and upper tertile boundaries of a sorted-in-place copy.

    Unit-weighted: a cell holding a thousand units moves the boundary more than
    one holding fifty, because the boundary is meant to describe the population
    a label will be applied to.

    **The two can coincide, and that is arithmetic rather than a fault.** These
    are order statistics over a heavily tied list — every unit in a cell carries
    that cell's rate — so with few distinct rates both indices can land inside
    the same block. Three equally sized cells do it every time: `n/3` sits just
    past the first block's end and `2n/3` just before the third's start, leaving
    both on the middle value. Six cells separate cleanly.

    When they coincide the two null-effect rules overlap, and rule order decides
    — Sure Thing before Lost Cause. The report states both boundaries so a
    reader can see when this has happened rather than infer it.
    """
    if not values:
        return 0, 0
    ordered = sorted(values)
    total = len(ordered)
    return ordered[total // 3], ordered[(2 * total) // 3]


def derive_thresholds(
    model: FoldModel,
    training_units: Sequence[Unit],
) -> FoldThresholds:
    """The fold's boundaries, from its training folds and nothing else.

    * **Self-recovery ceiling** is the training holdout arm's overall recovery
      rate — definitionally "what arrives without help", observed rather than
      chosen.
    * **Tertiles** are of the control rate each training unit would be scored
      at, so the boundaries describe the population, not the cell list.
    * **Harm threshold** is the upper bound of the training population's
      harm-uplift interval. A cell whose harm exceeds what the whole population's
      interval admits is genuinely unusual, and the bound is already computed by
      the same bootstrap everything else uses.
    """
    ceiling = model.global_counts.p_control_bps

    control_rates: list[int] = []
    for unit in training_units:
        resolution = model.resolution_for(unit)
        if resolution.counts is not None:
            control_rates.append(resolution.counts.p_control_bps)

    low, high = _tertiles(control_rates)

    harm = model.global_harm_counts
    harm_interval = bootstrap_interval(
        _bernoulli_arm(harm.recovered_treated, harm.n_treated),
        _bernoulli_arm(harm.recovered_holdout, harm.n_holdout),
        uplift_statistic,
        alpha_bps=model.alpha_bps,
        resamples=model.resamples,
        seed=model.seed,
    )

    return FoldThresholds(
        fold=model.fold,
        self_recovery_ceiling_bps=ceiling,
        low_tertile_bps=low,
        high_tertile_bps=high,
        harm_threshold_bps=harm_interval.high,
        training_size=model.training_size,
        qualifying_units=len(control_rates),
    )


# -- the rules ------------------------------------------------------------


def classify(uplift: UpliftScore, thresholds: FoldThresholds) -> tuple[Quadrant, str]:
    """Apply the five rules in order. First match wins.

    The order is the contract, not an implementation detail. Qualification comes
    first so a thin cell can never be labelled; harm comes before persuasion so
    a cell that both lifts recovery and destroys mandates is called what it is;
    and the two null-result rules come last, because they are the weakest claims
    in the set.
    """
    if not uplift.qualified:
        return Quadrant.GRAY_ZONE, RULE_NOT_QUALIFYING

    if uplift.ci_high_bps < 0:
        return Quadrant.SLEEPING_DOG, RULE_NEGATIVE_UPLIFT
    if uplift.harm_uplift_bps > thresholds.harm_threshold_bps:
        return Quadrant.SLEEPING_DOG, RULE_HARMFUL

    if uplift.ci_low_bps > 0 and uplift.p_control_bps < thresholds.self_recovery_ceiling_bps:
        return Quadrant.PERSUADABLE, RULE_SIGNIFICANT_UPLIFT

    contains_zero = uplift.interval.contains_zero
    if contains_zero and uplift.p_control_bps >= thresholds.high_tertile_bps:
        return Quadrant.SURE_THING, RULE_HIGH_BASELINE
    if contains_zero and uplift.p_control_bps <= thresholds.low_tertile_bps:
        return Quadrant.LOST_CAUSE, RULE_LOW_BASELINE

    return Quadrant.GRAY_ZONE, RULE_UNDECIDED


# -- assignment -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuadrantAssignment:
    """One unit's label, with everything needed to explain it."""

    risk_id: uuid.UUID
    quadrant: Quadrant
    rule: str
    fold: int
    uplift: UpliftScore

    @property
    def is_actionable(self) -> bool:
        """Only one label means act. The gate itself is a later phase."""
        return self.quadrant is Quadrant.PERSUADABLE

    def as_dict(self) -> dict[str, object]:
        return {
            "risk_id": str(self.risk_id),
            "quadrant": self.quadrant.value,
            "rule": self.rule,
            "fold": self.fold,
            "uplift_bps": self.uplift.uplift_bps,
            "uplift_ci_low_bps": self.uplift.ci_low_bps,
            "uplift_ci_high_bps": self.uplift.ci_high_bps,
            "p_control_bps": self.uplift.p_control_bps,
            "harm_uplift_bps": self.uplift.harm_uplift_bps,
            "qualified": self.uplift.qualified,
        }


@dataclass(frozen=True, slots=True)
class QuadrantRun:
    """Every unit's label, plus the per-fold boundaries behind them."""

    assignments: tuple[QuadrantAssignment, ...]
    thresholds: tuple[FoldThresholds, ...]

    @property
    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {quadrant.value: 0 for quadrant in Quadrant}
        for assignment in self.assignments:
            tally[assignment.quadrant.value] += 1
        return tally

    @property
    def rule_counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for assignment in self.assignments:
            tally[assignment.rule] = tally.get(assignment.rule, 0) + 1
        return tally

    def as_dict(self) -> dict[str, object]:
        return {
            "n": len(self.assignments),
            "counts": self.counts,
            "rule_counts": self.rule_counts,
            "thresholds": [t.as_dict() for t in self.thresholds],
        }


def assign_quadrants(
    units: Sequence[Unit],
    experiment_id: uuid.UUID,
    *,
    alpha_bps: int,
    mde_bps: int,
    folds: int = DEFAULT_FOLD_COUNT,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> QuadrantRun:
    """Cross-fit, derive each fold's boundaries, and label its held-out units.

    Ordered by `risk_id` so a rerun is byte-identical.
    """
    if not units:
        return QuadrantRun(assignments=(), thresholds=())
    if folds < 2:
        raise QuadrantError(f"cross-fitting needs at least two folds, got {folds}")

    membership: dict[int, list[Unit]] = {}
    for unit in units:
        membership.setdefault(fold_of(unit.risk_id, experiment_id, folds=folds), []).append(unit)

    assignments: list[QuadrantAssignment] = []
    all_thresholds: list[FoldThresholds] = []

    for fold in sorted(membership):
        model = fit_fold(
            units,
            experiment_id,
            fold,
            alpha_bps=alpha_bps,
            mde_bps=mde_bps,
            folds=folds,
            resamples=resamples,
            seed=seed,
        )
        training = [
            unit for unit in units if fold_of(unit.risk_id, experiment_id, folds=folds) != fold
        ]
        thresholds = derive_thresholds(model, training)
        all_thresholds.append(thresholds)

        for unit in membership[fold]:
            result = score(unit, model)
            quadrant, rule = classify(result, thresholds)
            assignments.append(
                QuadrantAssignment(
                    risk_id=unit.risk_id,
                    quadrant=quadrant,
                    rule=rule,
                    fold=fold,
                    uplift=result,
                )
            )

    return QuadrantRun(
        assignments=tuple(sorted(assignments, key=lambda item: item.risk_id)),
        thresholds=tuple(all_thresholds),
    )


__all__ = [
    "BPS_SCALE",
    "RULE_HARMFUL",
    "RULE_HIGH_BASELINE",
    "RULE_LOW_BASELINE",
    "RULE_NEGATIVE_UPLIFT",
    "RULE_NOT_QUALIFYING",
    "RULE_SIGNIFICANT_UPLIFT",
    "RULE_UNDECIDED",
    "FoldThresholds",
    "QuadrantAssignment",
    "QuadrantError",
    "QuadrantRun",
    "assign_quadrants",
    "classify",
    "derive_thresholds",
]
