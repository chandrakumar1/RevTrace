"""The T-learner: two conditional recovery models, one per arm.

    uplift(x) = p_treat(x) - p_control(x)

Both halves are **empirical cell rates** — the recovery rate among training
units of that arm in that cell. No fitting, no coefficients, no dependency, and
no float: two lookup tables of integer basis points. The features here are
already categorical, so a rate per cell is not a simplification of a model, it
*is* the model, and it has the useful property that every prediction can be
traced to a count a reader can check.

**Cross-fitted, always.** A unit is scored by a model built from the four folds
it is not in, so no prediction is influenced by the outcome it is predicting.
Fitting and scoring on the same rows would make every downstream number —
uplift, ranking, Qini — optimistic by an amount nobody could measure afterwards.

**Intention-to-treat, exactly as stored.** The arm comes from
`case_assignments` and is never re-derived. A unit whose execution failed is a
treated unit with a disappointing outcome, and moving it to the control arm
would let the treatment look best precisely where it worked least. This module
does not read `execution_failed` at all.

**Thin cells do not get a label.** Qualification is `cells.qualification_reason`,
unchanged: the exact unequal-allocation power requirement at the cell's own arm
ratio, with empty arms and impossible baselines refused first. A unit whose fine
cell is thin backs off to the coarse cell; if that is thin too it falls to the
global training rates and is marked **non-qualifying**, because a global average
is not a conditional estimate and must not be dressed up as one.

Intervals come from the pre-registered percentile bootstrap, seeded and at the
pre-registered resample count. A cell is bootstrapped **once per fold** and
reused across every unit that lands in it, which is what keeps the cost
proportional to the number of cells rather than to the number of units.

Nothing here reads a `truth_*` column, imports the generator, or writes.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from sqlalchemy.orm import Session

from app.causal.analysis import load_population
from app.causal.cells import (
    BPS_SCALE,
    DEFAULT_FOLD_COUNT,
    LADDER,
    QUALIFIED,
    CellCounts,
    CellResolution,
    Unit,
    fold_of,
    load_features,
    resolve,
    tally,
)
from app.causal.estimators import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    Interval,
    bootstrap_interval,
    rate_bps,
)

#: Stamped on every score. Encodes the design, the fold count and the cell
#: ladder, so two runs of different shapes can never be confused for each other
#: in `uplift_scores.model_version`.
MODEL_VERSION = "cellrate-1/k5/fc+pm"

#: The cell a unit falls to when neither ladder level qualifies. Named rather
#: than left as `None` so a report can count it.
GLOBAL_CELL = "(global)"

#: Reason recorded when the global fallback was used.
GLOBAL_FALLBACK = "global_fallback"


class UpliftError(ValueError):
    """A score could not be produced."""


def uplift_statistic(sum_t: int, n_t: int, sum_h: int, n_h: int) -> int:
    """The bootstrap statistic: difference of two rounded rates, in bps.

    Identical to the one `rate_effect` uses for the population estimate, so a
    cell interval and the overall interval are the same construction at
    different scopes rather than two conventions.
    """
    return rate_bps(sum_t, n_t) - rate_bps(sum_h, n_h)


def _arm_values(hits: int, total: int) -> list[int]:
    """A Bernoulli arm as a 0/1 sequence.

    Only the multiset matters to a bootstrap — `bootstrap_interval` sorts each
    arm before resampling — so reconstructing from counts is exact, not an
    approximation, and saves carrying every unit into the resampler.
    """
    if hits > total or hits < 0:
        raise UpliftError(f"cannot build an arm of {total} from {hits} hits")
    return [1] * hits + [0] * (total - hits)


# -- one cell's model -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class CellModel:
    """Both arms of one cell, plus the interval on their difference."""

    key: str
    level: int | None
    level_name: str | None
    counts: CellCounts
    harm_counts: CellCounts
    interval: Interval
    qualified: bool
    reason: str

    @property
    def p_treat_bps(self) -> int:
        return self.counts.p_treat_bps

    @property
    def p_control_bps(self) -> int:
        return self.counts.p_control_bps

    @property
    def uplift_bps(self) -> int:
        return self.p_treat_bps - self.p_control_bps

    @property
    def harm_treat_bps(self) -> int:
        return self.harm_counts.p_treat_bps

    @property
    def harm_control_bps(self) -> int:
        return self.harm_counts.p_control_bps

    @property
    def harm_uplift_bps(self) -> int:
        """Estimated per cell, reported, and never persisted.

        A point estimate is all the quadrant rule needs: it compares this
        against a threshold derived from the population interval, so the cell
        does not need an interval of its own.
        """
        return self.harm_treat_bps - self.harm_control_bps

    @property
    def interval_brackets_estimate(self) -> bool:
        """Whether the percentile interval contains its own point estimate.

        Usually yes, but a percentile bootstrap does not guarantee it, and
        `uplift_scores` has a CHECK that does. Surfaced here so the persistence
        layer can decide what to do rather than discovering it at INSERT.
        """
        return self.interval.contains(self.uplift_bps)


# -- one fold's pair of models --------------------------------------------


def _harm_view(units: Sequence[Unit]) -> list[Unit]:
    """The same units with `harmed` in the outcome position.

    Lets the harm rates reuse `cells.tally` exactly rather than growing a
    second counting path that could drift from the first.
    """
    return [replace(unit, recovered=unit.harmed) for unit in units]


@dataclass(frozen=True, slots=True)
class FoldModel:
    """What one fold's four training folds learned.

    Cell intervals are bootstrapped on first use and cached, so a cell costs one
    bootstrap however many units land in it.
    """

    fold: int
    alpha_bps: int
    mde_bps: int
    resamples: int
    seed: int
    tallies: tuple[Mapping[str, CellCounts], ...]
    harm_tallies: tuple[Mapping[str, CellCounts], ...]
    global_counts: CellCounts
    global_harm_counts: CellCounts
    training_size: int
    _cache: dict[tuple[int | None, str], CellModel] = field(default_factory=dict, repr=False)
    _resolutions: dict[tuple[str, ...], CellResolution] = field(default_factory=dict, repr=False)

    def resolution_for(self, unit: Unit) -> CellResolution:
        """Where this unit's features land on the ladder, memoised by cell.

        Resolution is a pure function of the feature cell — two units in the
        same cell resolve identically by construction — but deciding it runs
        the power calculation, which bisects the fixed-point normal twice. Doing
        that per unit rather than per cell made a ten-thousand-unit fold take
        twenty seconds instead of under one, for identical answers.
        """
        signature = tuple(key(unit.features) for _, key in LADDER)
        cached = self._resolutions.get(signature)
        if cached is None:
            cached = resolve(unit, self.tallies, mde_bps=self.mde_bps)
            self._resolutions[signature] = cached
        return replace(cached, risk_id=unit.risk_id)

    @property
    def cells_resolved(self) -> int:
        """Distinct feature cells this fold has had to judge."""
        return len(self._resolutions)

    def cell_model(
        self,
        key: str,
        level: int | None,
        level_name: str | None,
        counts: CellCounts,
        harm_counts: CellCounts,
        reason: str,
    ) -> CellModel:
        cached = self._cache.get((level, key))
        if cached is not None:
            return cached

        model = CellModel(
            key=key,
            level=level,
            level_name=level_name,
            counts=counts,
            harm_counts=harm_counts,
            interval=bootstrap_interval(
                _arm_values(counts.recovered_treated, counts.n_treated),
                _arm_values(counts.recovered_holdout, counts.n_holdout),
                uplift_statistic,
                alpha_bps=self.alpha_bps,
                resamples=self.resamples,
                seed=self.seed,
            ),
            qualified=reason == QUALIFIED,
            reason=reason,
        )
        self._cache[(level, key)] = model
        return model

    @property
    def cells_bootstrapped(self) -> int:
        """How many distinct cells this fold has had to resample."""
        return len(self._cache)


def fit_fold(
    units: Sequence[Unit],
    experiment_id: uuid.UUID,
    fold: int,
    *,
    alpha_bps: int,
    mde_bps: int,
    folds: int = DEFAULT_FOLD_COUNT,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> FoldModel:
    """Build the two lookup tables from every fold except `fold`."""
    training = [u for u in units if fold_of(u.risk_id, experiment_id, folds=folds) != fold]
    if not training:
        raise UpliftError(f"fold {fold} has no training data")

    harm_units = _harm_view(training)

    return FoldModel(
        fold=fold,
        alpha_bps=alpha_bps,
        mde_bps=mde_bps,
        resamples=resamples,
        seed=seed,
        tallies=tuple(tally(training, key) for _, key in LADDER),
        harm_tallies=tuple(tally(harm_units, key) for _, key in LADDER),
        global_counts=tally(training, lambda _features: GLOBAL_CELL)[GLOBAL_CELL],
        global_harm_counts=tally(harm_units, lambda _features: GLOBAL_CELL)[GLOBAL_CELL],
        training_size=len(training),
    )


# -- scoring --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpliftScore:
    """One cross-fitted prediction, with everything behind it."""

    risk_id: uuid.UUID
    model_version: str
    fold: int
    level: int | None
    level_name: str | None
    cell_key: str
    p_treat_bps: int
    p_control_bps: int
    uplift_bps: int
    interval: Interval
    harm_uplift_bps: int
    qualified: bool
    reason: str
    n_treated: int
    n_holdout: int

    @property
    def is_global_fallback(self) -> bool:
        return self.cell_key == GLOBAL_CELL

    @property
    def ci_low_bps(self) -> int:
        return self.interval.low

    @property
    def ci_high_bps(self) -> int:
        return self.interval.high

    def as_dict(self) -> dict[str, object]:
        return {
            "risk_id": str(self.risk_id),
            "model_version": self.model_version,
            "fold": self.fold,
            "level": self.level,
            "level_name": self.level_name,
            "cell_key": self.cell_key,
            "p_treat_bps": self.p_treat_bps,
            "p_control_bps": self.p_control_bps,
            "uplift_bps": self.uplift_bps,
            "uplift_ci_low_bps": self.ci_low_bps,
            "uplift_ci_high_bps": self.ci_high_bps,
            "harm_uplift_bps": self.harm_uplift_bps,
            "qualified": self.qualified,
            "reason": self.reason,
            "n_treated": self.n_treated,
            "n_holdout": self.n_holdout,
        }


def score(unit: Unit, model: FoldModel) -> UpliftScore:
    """Score one unit against a model that never saw it.

    Walks the ladder through `cells.resolve`, and falls to the global training
    rates when neither level qualifies — marked non-qualifying, because an
    unconditional average is not a conditional estimate.
    """
    resolution = model.resolution_for(unit)

    if resolution.is_gray_zone:
        cell = model.cell_model(
            key=GLOBAL_CELL,
            level=None,
            level_name=None,
            counts=model.global_counts,
            harm_counts=model.global_harm_counts,
            reason=GLOBAL_FALLBACK,
        )
    else:
        assert resolution.key is not None and resolution.counts is not None
        harm_counts = model.harm_tallies[resolution.level or 0].get(resolution.key, CellCounts())
        cell = model.cell_model(
            key=resolution.key,
            level=resolution.level,
            level_name=resolution.level_name,
            counts=resolution.counts,
            harm_counts=harm_counts,
            reason=QUALIFIED,
        )

    return UpliftScore(
        risk_id=unit.risk_id,
        model_version=MODEL_VERSION,
        fold=model.fold,
        level=cell.level,
        level_name=cell.level_name,
        cell_key=cell.key,
        p_treat_bps=cell.p_treat_bps,
        p_control_bps=cell.p_control_bps,
        uplift_bps=cell.uplift_bps,
        interval=cell.interval,
        harm_uplift_bps=cell.harm_uplift_bps,
        qualified=cell.qualified,
        reason=cell.reason,
        n_treated=cell.counts.n_treated,
        n_holdout=cell.counts.n_holdout,
    )


def cross_fit(
    units: Sequence[Unit],
    experiment_id: uuid.UUID,
    *,
    alpha_bps: int,
    mde_bps: int,
    folds: int = DEFAULT_FOLD_COUNT,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> list[UpliftScore]:
    """Score every unit from a model built without it. Ordered by `risk_id`.

    Ordered so a rerun produces byte-identical output and the ranking the Qini
    curve will read is a function of the data rather than of row order.
    """
    if not units:
        return []
    if folds < 2:
        raise UpliftError(f"cross-fitting needs at least two folds, got {folds}")

    by_fold: dict[int, list[Unit]] = {}
    for unit in units:
        by_fold.setdefault(fold_of(unit.risk_id, experiment_id, folds=folds), []).append(unit)

    scores: list[UpliftScore] = []
    for fold in sorted(by_fold):
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
        scores.extend(score(unit, model) for unit in by_fold[fold])

    return sorted(scores, key=lambda item: item.risk_id)


# -- loading --------------------------------------------------------------


def load_units(session: Session, experiment_id: uuid.UUID) -> list[Unit]:
    """The scored population: assigned arm and outcome, joined to features.

    The arm and the outcome come from `analysis.load_population`, which refuses
    anything but a complete sealed population — so a model can never be fitted
    on a half-finished experiment.
    """
    population = load_population(session, experiment_id)
    features = load_features(session, experiment_id)

    missing = [row.risk_id for row in population.rows if row.risk_id not in features]
    if missing:
        raise UpliftError(
            f"{len(missing)} of {len(population.rows)} enrolled units have no observable "
            f"features (for example {missing[0]}); a model cannot silently skip them"
        )

    return [
        Unit(
            risk_id=row.risk_id,
            arm=row.arm,
            recovered=row.recovered,
            harmed=row.harm_mandate_cancelled,
            features=features[row.risk_id],
        )
        for row in population.rows
    ]


__all__ = [
    "BPS_SCALE",
    "GLOBAL_CELL",
    "GLOBAL_FALLBACK",
    "MODEL_VERSION",
    "CellModel",
    "FoldModel",
    "UpliftError",
    "UpliftScore",
    "cross_fit",
    "fit_fold",
    "load_units",
    "score",
    "uplift_statistic",
]
