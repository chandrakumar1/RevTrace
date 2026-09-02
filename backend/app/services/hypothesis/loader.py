"""Stored experiment data to the one payload a model may see.

`HypothesisRequest` is assembled here and never accepted from a caller. That is
the point of the module: `validate()` checks a proposal *against the request*,
so a caller who supplied the request would be supplying the ground truth its own
answer is graded on. The only inputs are an experiment id and estimator
settings; everything else is read.

**No ground truth can reach the model, structurally.** `CellStat` has thirteen
integer/bool/controlled-string fields and nowhere to put a `truth_*` column, a
`risk_id`, a customer, an order or an amount. This module reads `app.causal`
aggregates and never `simulator`, never `app.reporting`, and never a raw row.

**Cell statistics come from the causal layer's own objects.** `CellModel` is
built by `FoldModel.cell_model`, which is what `uplift.score` calls; this module
walks the same two public methods in the same order rather than recomputing any
arithmetic. A test pins each emitted `CellStat` against `uplift.score`'s own
output for the same unit, so a divergence fails loudly instead of quietly
showing the model different numbers than the estimator used.

**The two ladder vocabularies are parallel by index, not by name.** The causal
ladder names its rungs by their key expression — `'failure_code|payment_method'`
and `'failure_code'` — while the contract names them `'fine'` and `'coarse'`.
They are mapped by position and never by string, because either side could be
reworded without the other noticing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import fields as dataclass_fields
from dataclasses import replace

from sqlalchemy.orm import Session

from app.agents.contracts import LADDER_LEVELS, CellStat, HypothesisRequest, PopulationSummary
from app.causal.analysis import itt_sample, load_outcome_rows
from app.causal.cells import QUALIFIED, CellCounts, Features, fold_of
from app.causal.estimators import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, bootstrap_interval
from app.causal.uplift import (
    DEFAULT_FOLD_COUNT,
    GLOBAL_CELL,
    GLOBAL_FALLBACK,
    CellModel,
    FoldModel,
    Unit,
    fit_fold,
    load_units,
    uplift_statistic,
)

#: The observable field names a cell key is made of. Derived from `Features`
#: rather than written out, so a field added there cannot silently go
#: unmentioned — and a `truth_*` field could not be added there without failing
#: the guard below.
FEATURE_VOCABULARY: tuple[str, ...] = tuple(f.name for f in dataclass_fields(Features))

#: A name starting with this is simulator ground truth. It has no route into
#: `Features` today; the check exists so that if one is ever added, this module
#: refuses rather than forwards it.
TRUTH_PREFIX = "truth_"


class LoaderError(ValueError):
    """The request could not be assembled, so nothing may be asked."""


def _ladder_level(cell: CellModel) -> str:
    """The contract's rung name for a cell, by **index**.

    `CellModel.level_name` is the ladder's key *expression* —
    `'failure_code|payment_method'` and `'failure_code'` — not the contract's
    rung name. Passing it through produced `unknown ladder level
    'failure_code|payment_method'`, caught by `CellStat.__post_init__`. The two
    vocabularies are parallel by index and only by index, so the index is what
    is used; the names are deliberately not matched on, since either side could
    be reworded without the other noticing.
    """
    if cell.level is None:  # pragma: no cover - callers exclude the global cell
        raise LoaderError(
            f"cell {cell.key!r} is the global fallback and sits on no ladder rung; "
            "it must be excluded before it reaches the contract"
        )
    try:
        return LADDER_LEVELS[cell.level]
    except IndexError as exc:
        raise LoaderError(
            f"cell {cell.key!r} is at ladder level {cell.level}, which has no name in "
            f"{LADDER_LEVELS}; the causal ladder and the agent contract have diverged"
        ) from exc


def _cell_stat(cell: CellModel) -> CellStat:
    """One `CellModel` as the contract the model sees.

    Only a real ladder cell reaches here. The global fallback is excluded
    upstream: it is not a cell but the unconditional training average used when
    nothing qualified, and presenting it as a rung would tell the model it is a
    conditional estimate when it is not.
    """
    return CellStat(
        cell_key=cell.key,
        ladder_level=_ladder_level(cell),
        n_treated=cell.counts.n_treated,
        n_holdout=cell.counts.n_holdout,
        recovered_treated=cell.counts.recovered_treated,
        recovered_holdout=cell.counts.recovered_holdout,
        p_treat_bps=cell.p_treat_bps,
        p_control_bps=cell.p_control_bps,
        uplift_bps=cell.uplift_bps,
        ci_low_bps=cell.interval.low,
        ci_high_bps=cell.interval.high,
        qualified=cell.qualified,
        qualification_reason=cell.reason,
    )


def _cell_for(unit: Unit, model: FoldModel) -> CellModel:
    """The cell that scores this unit.

    Mirrors the branch in `uplift.score` and calls the same two public methods
    on `FoldModel`, so the model sees the cell the estimator used rather than
    one reconstructed here.
    """
    resolution = model.resolution_for(unit)
    if resolution.is_gray_zone:
        return model.cell_model(
            key=GLOBAL_CELL,
            level=None,
            level_name=None,
            counts=model.global_counts,
            harm_counts=model.global_harm_counts,
            reason=GLOBAL_FALLBACK,
        )
    if resolution.key is None or resolution.counts is None:  # pragma: no cover - defensive
        raise LoaderError("a qualifying resolution carried no cell")
    harm_counts = model.harm_tallies[resolution.level or 0].get(resolution.key, CellCounts())
    return model.cell_model(
        key=resolution.key,
        level=resolution.level,
        level_name=resolution.level_name,
        counts=resolution.counts,
        harm_counts=harm_counts,
        reason=QUALIFIED,
    )


def collect_cells(
    units: Sequence[Unit],
    experiment_id: uuid.UUID,
    *,
    alpha_bps: int,
    mde_bps: int,
    folds: int = DEFAULT_FOLD_COUNT,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[CellStat, ...]:
    """Every distinct cell the population resolves to, ordered by key.

    Ordered so two runs over one population produce the same payload, which is
    what makes a recorded response replayable.

    A cell key may appear in more than one fold with **different counts**, since
    each fold is fitted without its own units. Keying on `(fold, key)` keeps
    those distinct; collapsing them would silently show the model one fold's
    numbers under another fold's name.

    **The global fallback is excluded.** A unit whose features qualified at no
    rung is scored against the unconditional training average, which is not a
    cell — it is the population. Showing it alongside real cells, under a rung
    name it does not have, would invite the model to claim that the population
    differs from the population. Units resolving to it are therefore
    unrepresented here, and a run where *every* unit does is refused by the
    caller rather than turned into an empty request.
    """
    if not units:
        raise LoaderError("no units: an experiment with no scored population has no cells")

    seen: dict[tuple[int, str], CellStat] = {}
    fallback_units = 0
    by_fold: dict[int, list[Unit]] = {}
    for unit in units:
        by_fold.setdefault(fold_of(unit.risk_id, experiment_id, folds=folds), []).append(unit)

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
        for unit in by_fold[fold]:
            cell = _cell_for(unit, model)
            if cell.level is None:
                fallback_units += 1
                continue
            seen.setdefault((fold, cell.key), _cell_stat(cell))

    if not seen:
        raise LoaderError(
            f"no qualifying ladder cells: all {fallback_units:,} scored units fell to the "
            f"global fallback ({GLOBAL_FALLBACK}), so there is no cell to form a hypothesis "
            f"about. Refusing rather than asking the model to compare the population with "
            f"itself."
        )

    # One key, several folds: disambiguated so `HypothesisRequest` still sees
    # distinct keys and the model can still name one exactly.
    counts: dict[str, int] = {}
    for _fold, key in seen:
        counts[key] = counts.get(key, 0) + 1

    stats: list[CellStat] = []
    for (fold, key), stat in sorted(seen.items()):
        if counts[key] > 1:
            stat = replace(stat, cell_key=f"{key}@fold{fold}")
        stats.append(stat)
    return tuple(sorted(stats, key=lambda item: item.cell_key))


def load_population_summary(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    alpha_bps: int,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> PopulationSummary:
    """The population effect every cell is compared against.

    Estimated here from `app.causal` rather than read from the evaluation
    report: an `app/` module may not import the reporter, which is the only
    permitted reader of ground truth, and this module must be able to run
    without one.
    """
    rows = load_outcome_rows(session, experiment_id)
    sample = itt_sample(rows, experiment_id)
    treatment, holdout = sample.treatment, sample.holdout

    if treatment.is_empty or holdout.is_empty:
        raise LoaderError(
            f"experiment {experiment_id} has an empty arm "
            f"(treatment={treatment.n}, holdout={holdout.n}); an effect needs both"
        )

    interval = bootstrap_interval(
        list(treatment.recovered),
        list(holdout.recovered),
        uplift_statistic,
        alpha_bps=alpha_bps,
        resamples=resamples,
        seed=seed,
    )
    ate = uplift_statistic(treatment.recoveries, treatment.n, holdout.recoveries, holdout.n)
    return PopulationSummary(
        ate_bps=ate,
        ci_low_bps=interval.low,
        ci_high_bps=interval.high,
        n_treatment=treatment.n,
        n_holdout=holdout.n,
        feature_vocabulary=FEATURE_VOCABULARY,
    )


def load_hypothesis_request(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    alpha_bps: int,
    mde_bps: int,
    folds: int = DEFAULT_FOLD_COUNT,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> HypothesisRequest:
    """Assemble the whole payload. Reads only; writes nothing.

    Refuses rather than degrades: an empty population, an empty arm, a duplicate
    cell key or a truth-shaped feature name all raise here, because a request
    that is wrong in any of those ways would produce a proposal that looks
    ordinary and is graded against the wrong numbers.
    """
    leaked = [name for name in FEATURE_VOCABULARY if name.startswith(TRUTH_PREFIX)]
    if leaked:
        raise LoaderError(
            f"refusing to build a request: {leaked} are ground-truth fields and "
            f"must never be shown to a model"
        )

    units = load_units(session, experiment_id)
    if not units:
        raise LoaderError(f"experiment {experiment_id} has no scored units")

    population = load_population_summary(
        session, experiment_id, alpha_bps=alpha_bps, resamples=resamples, seed=seed
    )
    cells = collect_cells(
        units,
        experiment_id,
        alpha_bps=alpha_bps,
        mde_bps=mde_bps,
        folds=folds,
        resamples=resamples,
        seed=seed,
    )
    if not cells:  # pragma: no cover - collect_cells raises first
        raise LoaderError("no cells were resolved for this population")

    keys = [cell.cell_key for cell in cells]
    if len(set(keys)) != len(keys):  # pragma: no cover - disambiguated above
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        raise LoaderError(f"duplicate cell keys would make a proposal ambiguous: {duplicates}")

    # `HypothesisRequest.__post_init__` re-checks non-empty and distinct keys.
    # Both are checked here too, so the failure names this loader rather than a
    # contract the caller never touched.
    return HypothesisRequest(experiment_id=experiment_id, population=population, cells=cells)


__all__ = [
    "FEATURE_VOCABULARY",
    "TRUTH_PREFIX",
    "LoaderError",
    "collect_cells",
    "load_hypothesis_request",
    "load_population_summary",
]
