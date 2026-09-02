"""The evaluation report — and the only place the answer key may be read.

Section 10 of the pre-registration names exactly one permitted reader of the
`truth_*` columns, and this is it. The reason is narrow: an evaluation report
has to say how close the estimate came to the real effect, and that comparison
is impossible without the value being estimated. Every other module works from
observed outcomes alone, and a guard over the whole application package fails
the build if any of them names a truth column.

The direction of the dependency matters as much as the permission. This module
reads truth **out of the database**, never from the generator — it has no import
of it and could not acquire one, because the application package may not import
the generator at all. So the answer key reaches the report the same way any
other column does, and the estimator it scores has no path to it.

**Everything here is labelled synthetic.** The header says so, the JSON says so,
and the limitations section says why it has to: the planted effects are
assumptions someone wrote down, not measurements, and recovering them validates
the estimator rather than the world.

**Nothing is reported without an interval**, and nothing is reported that cannot
be computed from inputs that exist. Section 6 of the plan lists thirteen
required sections; the ones needing a model, an economic assumption, or a
subsystem that has not been built are listed as deferred with the reason, rather
than filled with a plausible-looking zero.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.causal.analysis import AnalysisPopulation, ArmSample, load_population
from app.causal.balance import BalanceReport, report_for_experiment
from app.causal.estimators import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    AmountEffect,
    RateEffect,
    amount_effect,
    rate_bps,
    rate_effect,
)
from app.causal.power import is_underpowered, mde_for_n, required_n_per_arm
from app.causal.qini import TOP_SHARE_BPS, Capture, QiniResult, RankedUnit, rank, round_half_up
from app.causal.qini import evaluate as qini_evaluate
from app.causal.quadrants import FoldThresholds, QuadrantAssignment, assign_quadrants
from app.causal.uplift import DEFAULT_FOLD_COUNT, GLOBAL_CELL, MODEL_VERSION, load_units
from app.models import CaseAssignment, CaseOutcome, Experiment
from app.models.enums import Quadrant

#: Full basis-point scale, matching every module in `app.causal`.
BPS_SCALE = 10_000

#: Stamped on every rendering, in the header and in the payload. Results here
#: come from a generator, and a reader must never have to infer that.
SYNTHETIC_LABEL = "SYNTHETIC / DEMO EVALUATION"

#: Sections of the required contents that Day 4 cannot honestly produce, with
#: the reason each is missing. Listed in the report rather than omitted: a gap
#: a reader can see is a gap; a gap they cannot see is a claim.
DEFERRED_SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "Detection precision and recall",
        "the detection benchmark harness lives in the test tree, and the application "
        "may not import from it; promoting it is scheduled separately",
    ),
    (
        "Net incremental value P&L",
        "requires a gross margin and an average customer lifetime value. Neither "
        "exists anywhere in this codebase, and inventing either would put a made-up "
        "number into a money figure",
    ),
    (
        "Harm cost",
        "same missing inputs as the P&L. The harm *effect* is reported below with an "
        "interval; only its conversion into money is deferred",
    ),
    (
        "Failure scenarios and observed behaviour",
        "requires the provider adapter and webhook verification, which are a later phase",
    ),
)

#: Stated in every report, because they are what make the rest credible.
LIMITATIONS: tuple[str, ...] = (
    "All results are synthetic. The planted parameters are assumptions someone "
    "wrote down, not measurements.",
    "The effects are planted, so recovering them validates the estimator, not the world.",
    "Real customer response to a nudge is unobservable in a test environment.",
    "The comparison against the true effect is only possible because the true effect "
    "was written by hand. No production system can produce this section.",
    "A production holdout costs real money. The sizing calculator quantifies that "
    "trade-off; it does not remove it.",
    "Interval calibration was checked on 20 independently seeded populations of 2,000 "
    "and the true effect fell inside the 95% interval every time. That is no evidence "
    "of under-coverage; it is not a demonstration of exact calibration. Twenty trials "
    "cannot separate 95% from 99% — under a true 95% rate, a clean sweep is the single "
    "most likely outcome (p = 0.36), and the one-sided lower bound on true coverage is "
    "only 86%. Reporting it as '100% calibrated' would overstate what was measured.",
    "The generator's observable signal was deliberately strengthened after two planted "
    "strata proved indistinguishable from the features being persisted: `failure_code` "
    "is now written with 70% characteristic / 30% off-characteristic noise. The model "
    "was never changed to fit the data, but the data was made learnable, and a real "
    "payment stream carries whatever signal it carries.",
    "The rate/mix split is an accounting identity, not a causal decomposition. The rate "
    "effect prices the recovery-rate lift at the holdout's mean order value, and "
    "everything left over is called mix, so that component absorbs genuine composition "
    "shifts and the single rounding together. No third interaction term is reported; "
    "carving one out would need a convention nobody has agreed. Where most of the "
    "incremental figure is mix rather than rate, the result depended on *which* orders "
    "were recovered as much as on how many, and it need not reproduce under a different "
    "order distribution.",
)

#: Limitations that apply only where an uplift model was actually fitted. Kept
#: apart from the general list so a Day 4 report does not disclaim a model it
#: never ran.
UPLIFT_LIMITATIONS: tuple[str, ...] = (
    "Cell-level intervals are nominal 95% and uncorrected for multiplicity. Nine cells "
    "are scored per fold and each interval is read on its own, so the chance that at "
    "least one is wrong is far above 5%. The Benjamini-Hochberg procedure exists in the "
    "estimator layer and is deliberately not applied to per-cell quadrant decisions; "
    "treat a single cell's interval as indicative, not as a test that survived "
    "correction. The quadrant labels that follow from these intervals are therefore "
    "operational decisions — they say which cells to act on given what was measured — "
    "not statistical findings that survived corrected testing.",
    "Sure Thing and Lost Cause are defined as a confidence interval containing zero. At "
    "N=10,000 no cell's interval contains zero, so both quadrants come out empty — the "
    "definition asks for a null result, and a study this size resolves effects a "
    "smaller one would have missed. Distinguishing 'no effect' from 'an effect too "
    "small to be worth acting on' needs equivalence testing against a pre-declared "
    "margin, which is future work. The margin proposed for that work is |uplift| < 50 "
    "bps; it was not pre-registered for this run and did not determine any result "
    "reported here. The classifier is unchanged; this is a limit of the definition, not "
    "a defect in the run.",
    "Quadrant labels are only as sharp as the features. `intentional_churner` and "
    "`expired_or_blocked_card` share the failure code `card_declined`, and no persisted "
    "feature separates them, so the merged cell inherits the blocked-card lift and the "
    "churner is labelled Persuadable against its planted Lost Cause. This is a "
    "feature-resolution limit. It would be resolved by a feature that distinguishes the "
    "two, never by retuning the model against the answer key.",
    "Top-share capture by unit count is not a revenue claim. The top 20% of the ranking "
    "holds 38.66% of incremental recoveries but only 26.94% of incremental rupees, so "
    "the ranking is better at finding recoveries than at finding valuable ones. Quote "
    "the amount-weighted figure whenever the claim is about money.",
)


class EvaluationError(ValueError):
    """The report could not be produced."""


# -- ground truth: the only truth reader in the application ---------------


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """What the generator knows and no real system could observe.

    Both potential outcomes for the same unit — what it would have done treated
    *and* untreated. Read here, reported here, and nowhere else.
    """

    n: int
    y0_rate_bps: int
    y1_rate_bps: int
    true_ate_bps: int
    harm0_rate_bps: int
    harm1_rate_bps: int
    true_harm_ate_bps: int
    self_recovery_share_bps: int

    def as_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "y0_rate_bps": self.y0_rate_bps,
            "y1_rate_bps": self.y1_rate_bps,
            "true_ate_bps": self.true_ate_bps,
            "harm0_rate_bps": self.harm0_rate_bps,
            "harm1_rate_bps": self.harm1_rate_bps,
            "true_harm_ate_bps": self.true_harm_ate_bps,
            "self_recovery_share_bps": self.self_recovery_share_bps,
        }


@dataclass(frozen=True, slots=True)
class StratumTruth:
    """The planted effect within one behavioural stratum."""

    label: str
    n: int
    true_ate_bps: int
    true_harm_ate_bps: int


def _truth_rows(session: Session, experiment_id: uuid.UUID) -> list[CaseOutcome]:
    statement = (
        select(CaseOutcome)
        .join(CaseAssignment, CaseAssignment.risk_id == CaseOutcome.risk_id)
        .where(CaseAssignment.experiment_id == experiment_id)
        .order_by(CaseOutcome.risk_id)
    )
    return list(session.execute(statement).scalars())


def ground_truth(session: Session, experiment_id: uuid.UUID) -> GroundTruth:
    """The known effect over the whole enrolled population.

    Computed the way the generator computes it — a difference of two rounded
    rates — so the known value and the measured value are comparable to the
    last basis point rather than differing by a rounding convention.
    """
    rows = _truth_rows(session, experiment_id)
    if not rows:
        raise EvaluationError(f"experiment {experiment_id} has no outcomes to score against")
    if any(row.truth_y0 is None or row.truth_y1 is None for row in rows):
        raise EvaluationError(
            f"experiment {experiment_id} carries no ground truth; only a generated "
            "population can be scored against a known effect"
        )

    total = len(rows)
    y0 = rate_bps(sum(1 for row in rows if row.truth_y0), total)
    y1 = rate_bps(sum(1 for row in rows if row.truth_y1), total)
    harm0 = rate_bps(sum(1 for row in rows if row.truth_harm_0), total)
    harm1 = rate_bps(sum(1 for row in rows if row.truth_harm_1), total)

    return GroundTruth(
        n=total,
        y0_rate_bps=y0,
        y1_rate_bps=y1,
        true_ate_bps=y1 - y0,
        harm0_rate_bps=harm0,
        harm1_rate_bps=harm1,
        true_harm_ate_bps=harm1 - harm0,
        self_recovery_share_bps=y0,
    )


def ground_truth_by_stratum(session: Session, experiment_id: uuid.UUID) -> tuple[StratumTruth, ...]:
    """The known effect within each planted stratum, alphabetically.

    Only valid because the data is synthetic — the labels are the generator's
    own, and no production system has them.
    """
    grouped: dict[str, list[CaseOutcome]] = {}
    for row in _truth_rows(session, experiment_id):
        grouped.setdefault(row.truth_segment or "(unlabelled)", []).append(row)

    results: list[StratumTruth] = []
    for label in sorted(grouped):
        rows = grouped[label]
        total = len(rows)
        y0 = rate_bps(sum(1 for row in rows if row.truth_y0), total)
        y1 = rate_bps(sum(1 for row in rows if row.truth_y1), total)
        harm0 = rate_bps(sum(1 for row in rows if row.truth_harm_0), total)
        harm1 = rate_bps(sum(1 for row in rows if row.truth_harm_1), total)
        results.append(
            StratumTruth(
                label=label,
                n=total,
                true_ate_bps=y1 - y0,
                true_harm_ate_bps=harm1 - harm0,
            )
        )
    return tuple(results)


# -- uplift reporting -----------------------------------------------------

#: The three planted strata the acceptance criterion is stated over.
#:
#: The criterion circulated for a time naming `chronic_bank_decline` as the
#: persuadable arm. **No such stratum has ever existed** — not in the
#: generator's stratum registry, not in any commit. The name was invented in an
#: earlier proposal and adopted in good faith from it.
#:
#: The correction is settled by the same proposal's own wording: it said
#: **stratum 2**, and the registry numbers stratum 2
#: `insufficient_funds_salary_cycle`. That is the reference, and it is chosen on
#: provenance rather than on realised effect size. Three of the seven strata
#: declare `expected_quadrant=PERSUADABLE` (2, 3 and 7), so effect size cannot
#: identify one, and picking the largest would be reasoning from the answer the
#: criterion is supposed to test.
#:
#: Stratum 2 is a good comparison on its own terms: the registry plants its
#: heterogeneity conditional on `salary_window`, an *observable* the model can
#: actually learn, rather than on the stratum label it never sees.
#:
#: The registry is named by path nowhere in this file on purpose. An isolation
#: guard scans `app/` for that module name as a raw substring, and it cannot
#: tell a citation in a comment from an import — so prose explaining why the
#: import must never exist would trip it.
SEGMENT_SLEEPING_DOG = "low_engagement_mandate_holder"
SEGMENT_SELF_RECOVERING = "transient_upi_timeout"
SEGMENT_PERSUADABLE = "insufficient_funds_salary_cycle"

#: Quadrant labels in enum order, so an empty quadrant still occupies a column
#: rather than vanishing from the matrix. Two of the five come out empty on the
#: benchmark, and a reader must be able to see that they were looked for.
QUADRANT_ORDER: tuple[str, ...] = tuple(quadrant.value for quadrant in Quadrant)


def _mean_bps(values: Sequence[int]) -> int:
    """Integer mean, halves away from zero.

    Uses the Qini module's rounding helper rather than `//`, which floors toward
    negative infinity and would bias every sleeping-dog mean downward.
    """
    if not values:
        return 0
    return round_half_up(sum(values), len(values))


def _modal(counts: Mapping[str, int]) -> str:
    """The most common label, ties broken by enum order.

    Deterministic on purpose: a tie resolved by dictionary insertion order would
    make the acceptance criterion depend on the order rows came back in.
    """
    best, best_count = QUADRANT_ORDER[0], -1
    for label in QUADRANT_ORDER:
        if counts.get(label, 0) > best_count:
            best, best_count = label, counts.get(label, 0)
    return best


@dataclass(frozen=True, slots=True)
class SegmentRow:
    """One planted stratum's row of the confusion matrix.

    `truth_segment` is read to build this and for nothing else. It never reaches
    the model: the labels being counted here were produced by a scorer that saw
    only observed features, and this comparison happens strictly afterwards.
    """

    label: str
    n: int
    counts: dict[str, int]
    modal_quadrant: str
    mean_uplift_bps: int
    mean_harm_uplift_bps: int

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "n": self.n,
            "counts": dict(self.counts),
            "modal_quadrant": self.modal_quadrant,
            "mean_uplift_bps": self.mean_uplift_bps,
            "mean_harm_uplift_bps": self.mean_harm_uplift_bps,
        }


@dataclass(frozen=True, slots=True)
class ConfusionMatrix:
    """Assigned quadrant against planted stratum. Synthetic data only.

    The rows are `strata`, matching `StratumTruth` and `ground_truth_by_stratum`
    elsewhere in this module. The other obvious plural is also the name of a
    generator module, and the isolation guard over `app/` scans for that name as
    a raw substring — it cannot tell a field name from an import, so the word is
    avoided here rather than the guard weakened.
    """

    strata: tuple[SegmentRow, ...]
    quadrants: tuple[str, ...] = QUADRANT_ORDER

    def segment(self, label: str) -> SegmentRow | None:
        return next((row for row in self.strata if row.label == label), None)

    @property
    def empty_quadrants(self) -> tuple[str, ...]:
        """Quadrants no unit landed in. Reported, not hidden."""
        return tuple(
            label
            for label in self.quadrants
            if sum(row.counts.get(label, 0) for row in self.strata) == 0
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "quadrants": list(self.quadrants),
            "strata": [row.as_dict() for row in self.strata],
            "empty_quadrants": list(self.empty_quadrants),
        }


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    """One clause of the acceptance criterion, and whether it held."""

    name: str
    expected: str
    observed: str
    passed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "expected": self.expected,
            "observed": self.observed,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class GrayZoneBreakdown:
    """Why each Gray Zone unit ended up there.

    Two different reasons produce the same label — a cell that never qualified,
    and a qualified cell whose result matched no rule. Collapsing them would
    hide which of the two the model is actually doing.
    """

    total: int
    by_rule: dict[str, int]
    by_reason: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "by_rule": dict(self.by_rule),
            "by_reason": dict(self.by_reason),
        }


@dataclass(frozen=True, slots=True)
class LadderUsage:
    """Which rung of the cell ladder each unit was scored at."""

    by_level: dict[str, int]
    distinct_cells: int
    cells: tuple[tuple[str, int], ...]
    global_fallbacks: int

    def as_dict(self) -> dict[str, object]:
        return {
            "by_level": dict(self.by_level),
            "distinct_cells": self.distinct_cells,
            "cells": [{"cell": key, "n": n} for key, n in self.cells],
            "global_fallbacks": self.global_fallbacks,
        }


@dataclass(frozen=True, slots=True)
class HarmUpliftSummary:
    """The harm effect that decided sleeping-dog labels and was then discarded.

    Held in memory only — there is no column for it, by design — so the report
    is the only place it is ever visible.
    """

    n: int
    min_bps: int
    max_bps: int
    mean_bps: int
    positive: int
    above_fold_threshold: int

    def as_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "min_bps": self.min_bps,
            "max_bps": self.max_bps,
            "mean_bps": self.mean_bps,
            "positive": self.positive,
            "above_fold_threshold": self.above_fold_threshold,
        }


@dataclass(frozen=True, slots=True)
class AmountCapture:
    """Top-share capture weighted by money rather than by unit count.

    A model can rank well on recoveries and badly on rupees if the units it
    promotes are cheap ones, so the count-weighted figure alone can flatter a
    ranking that recovers nothing worth having.
    """

    share_bps: int
    k: int
    n: int
    qini_at_k: int
    total: int
    capture_bps: int | None

    @property
    def is_defined(self) -> bool:
        return self.capture_bps is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "share_bps": self.share_bps,
            "k": self.k,
            "n": self.n,
            "qini_at_k": self.qini_at_k,
            "total": self.total,
            "capture_bps": self.capture_bps,
        }


def amount_capture(
    ranked: Sequence[RankedUnit],
    amounts: Mapping[uuid.UUID, int],
    *,
    share_bps: int = TOP_SHARE_BPS,
) -> AmountCapture:
    """`Q(k)` over recovered amounts, using the ranking Qini already produced.

    Mirrors `qini.qini_curve` term for term — the same holdout correction, the
    same half-up rounding, imported from that module so the two conventions
    cannot drift apart. Implemented separately rather than reused because
    `RankedUnit.recovered` is a boolean, and pushing money through it would make
    the type a lie.
    """
    treated_amount = holdout_amount = 0
    n_treated = n_holdout = 0
    values: list[int] = []

    for unit in ranked:
        amount = amounts.get(unit.risk_id, 0)
        if unit.is_treatment:
            n_treated += 1
            treated_amount += amount
        else:
            n_holdout += 1
            holdout_amount += amount

        if n_holdout == 0:
            values.append(treated_amount)
        else:
            values.append(treated_amount - round_half_up(holdout_amount * n_treated, n_holdout))

    n = len(values)
    total = values[-1] if values else 0
    k = n * share_bps // 10_000
    at_k = 0 if k == 0 else values[k - 1]

    capture_bps = None
    if total != 0:
        sign = -1 if total < 0 else 1
        capture_bps = round_half_up(10_000 * at_k * sign, abs(total))

    return AmountCapture(
        share_bps=share_bps,
        k=k,
        n=n,
        qini_at_k=at_k,
        total=total,
        capture_bps=capture_bps,
    )


@dataclass(frozen=True, slots=True)
class UpliftReport:
    """Everything Day 5 measured about one scored population."""

    model_version: str
    folds: int
    alpha_bps: int
    mde_bps: int
    resamples: int
    seed: int
    n_scored: int

    qini: QiniResult
    top_capture: Capture
    top_amount_capture: AmountCapture

    quadrant_counts: dict[str, int]
    rule_counts: dict[str, int]
    gray_zone: GrayZoneBreakdown
    ladder: LadderUsage
    harm: HarmUpliftSummary
    thresholds: tuple[FoldThresholds, ...]
    confusion: ConfusionMatrix
    acceptance: tuple[AcceptanceCriterion, ...]

    @property
    def accepted(self) -> bool:
        """Every clause held. Reported as measured, never forced."""
        return all(criterion.passed for criterion in self.acceptance)

    def as_dict(self) -> dict[str, object]:
        return {
            "model": {
                "version": self.model_version,
                "folds": self.folds,
                "alpha_bps": self.alpha_bps,
                "mde_bps": self.mde_bps,
                "resamples": self.resamples,
                "seed": self.seed,
                "n_scored": self.n_scored,
            },
            "qini": self.qini.as_dict(),
            "top_capture": self.top_capture.as_dict(),
            "top_amount_capture": self.top_amount_capture.as_dict(),
            "quadrant_counts": dict(self.quadrant_counts),
            "rule_counts": dict(self.rule_counts),
            "gray_zone": self.gray_zone.as_dict(),
            "ladder": self.ladder.as_dict(),
            "harm_uplift": self.harm.as_dict(),
            "fold_thresholds": [threshold.as_dict() for threshold in self.thresholds],
            "confusion_matrix": self.confusion.as_dict(),
            "acceptance": {
                "criteria": [criterion.as_dict() for criterion in self.acceptance],
                "accepted": self.accepted,
            },
            "limitations": list(UPLIFT_LIMITATIONS),
        }


def _gray_zone_breakdown(assignments: Sequence[QuadrantAssignment]) -> GrayZoneBreakdown:
    by_rule: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    total = 0
    for assignment in assignments:
        if assignment.quadrant is not Quadrant.GRAY_ZONE:
            continue
        total += 1
        by_rule[assignment.rule] = by_rule.get(assignment.rule, 0) + 1
        reason = assignment.uplift.reason
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return GrayZoneBreakdown(
        total=total,
        by_rule=dict(sorted(by_rule.items())),
        by_reason=dict(sorted(by_reason.items())),
    )


def _ladder_usage(assignments: Sequence[QuadrantAssignment]) -> LadderUsage:
    by_level: dict[str, int] = {}
    cells: dict[str, int] = {}
    fallbacks = 0
    for assignment in assignments:
        score = assignment.uplift
        level = score.level_name or GLOBAL_CELL
        by_level[level] = by_level.get(level, 0) + 1
        cells[score.cell_key] = cells.get(score.cell_key, 0) + 1
        if score.is_global_fallback:
            fallbacks += 1
    return LadderUsage(
        by_level=dict(sorted(by_level.items())),
        distinct_cells=len(cells),
        cells=tuple(sorted(cells.items(), key=lambda item: (-item[1], item[0]))),
        global_fallbacks=fallbacks,
    )


def _harm_summary(
    assignments: Sequence[QuadrantAssignment],
    thresholds: Sequence[FoldThresholds],
) -> HarmUpliftSummary:
    values = [assignment.uplift.harm_uplift_bps for assignment in assignments]
    if not values:
        return HarmUpliftSummary(0, 0, 0, 0, 0, 0)

    by_fold = {threshold.fold: threshold.harm_threshold_bps for threshold in thresholds}
    above = sum(
        1
        for assignment in assignments
        if assignment.uplift.harm_uplift_bps > by_fold.get(assignment.fold, 0)
    )
    return HarmUpliftSummary(
        n=len(values),
        min_bps=min(values),
        max_bps=max(values),
        mean_bps=_mean_bps(values),
        positive=sum(1 for value in values if value > 0),
        above_fold_threshold=above,
    )


def confusion_matrix(
    session: Session,
    experiment_id: uuid.UUID,
    assignments: Sequence[QuadrantAssignment],
) -> ConfusionMatrix:
    """Assigned quadrants cross-tabulated against the planted strata.

    **The only use of `truth_segment` in the application.** It is read here,
    after every label has already been decided, purely to say how well the
    labels line up with what was planted. The scorer that produced them has no
    path to this column — a guard over `app/causal/` fails the build if one
    appears.
    """
    labels = {
        row.risk_id: row.truth_segment or "(unlabelled)"
        for row in _truth_rows(session, experiment_id)
    }

    grouped: dict[str, list[QuadrantAssignment]] = {}
    for assignment in assignments:
        grouped.setdefault(labels.get(assignment.risk_id, "(unlabelled)"), []).append(assignment)

    rows: list[SegmentRow] = []
    for label in sorted(grouped):
        members = grouped[label]
        counts = {quadrant: 0 for quadrant in QUADRANT_ORDER}
        for assignment in members:
            counts[assignment.quadrant.value] += 1
        rows.append(
            SegmentRow(
                label=label,
                n=len(members),
                counts=counts,
                modal_quadrant=_modal(counts),
                mean_uplift_bps=_mean_bps([m.uplift.uplift_bps for m in members]),
                mean_harm_uplift_bps=_mean_bps([m.uplift.harm_uplift_bps for m in members]),
            )
        )
    return ConfusionMatrix(strata=tuple(rows))


def acceptance_criteria(matrix: ConfusionMatrix) -> tuple[AcceptanceCriterion, ...]:
    """The three clauses, evaluated against what was measured.

    Stated as an exclusion for the self-recovering segment rather than a
    specific label. The decision that matters is *do not spend money here*, and
    both Gray Zone and Sure Thing produce it; requiring one of them would assert
    something about the classifier's vocabulary the claim never needed.
    """
    dog = matrix.segment(SEGMENT_SLEEPING_DOG)
    self_recovering = matrix.segment(SEGMENT_SELF_RECOVERING)
    persuadable = matrix.segment(SEGMENT_PERSUADABLE)

    criteria: list[AcceptanceCriterion] = []

    observed = dog.modal_quadrant if dog else "segment absent"
    criteria.append(
        AcceptanceCriterion(
            name=f"{SEGMENT_SLEEPING_DOG} is modally a sleeping dog",
            expected=Quadrant.SLEEPING_DOG.value,
            observed=observed,
            passed=dog is not None and dog.modal_quadrant == Quadrant.SLEEPING_DOG.value,
        )
    )

    observed = self_recovering.modal_quadrant if self_recovering else "segment absent"
    criteria.append(
        AcceptanceCriterion(
            name=f"{SEGMENT_SELF_RECOVERING} is not modally persuadable",
            expected=f"anything but {Quadrant.PERSUADABLE.value}",
            observed=observed,
            passed=(
                self_recovering is not None
                and self_recovering.modal_quadrant != Quadrant.PERSUADABLE.value
            ),
        )
    )

    if self_recovering is None or persuadable is None:
        observed, passed = "segment absent", False
    else:
        observed = f"{self_recovering.mean_uplift_bps} bps vs {persuadable.mean_uplift_bps} bps"
        passed = self_recovering.mean_uplift_bps < persuadable.mean_uplift_bps
    criteria.append(
        AcceptanceCriterion(
            name=f"mean uplift ranks {SEGMENT_SELF_RECOVERING} below {SEGMENT_PERSUADABLE}",
            expected="strictly lower",
            observed=observed,
            passed=passed,
        )
    )

    return tuple(criteria)


def build_uplift_report(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    folds: int = DEFAULT_FOLD_COUNT,
    seed: int = BOOTSTRAP_SEED,
) -> UpliftReport:
    """Cross-fit, rank, label, and score the labels against the answer key.

    Reads only. No `uplift_scores` row is written here — persisting a scoring
    run is the repository's job and a separate decision, and a reporter that
    wrote rows as a side effect of being read would make every report a
    mutation.
    """
    experiment = session.get(Experiment, experiment_id)
    if experiment is None:
        raise EvaluationError(f"no experiment {experiment_id}")

    units = load_units(session, experiment_id)
    if not units:
        raise EvaluationError(f"experiment {experiment_id} has no scoreable units")

    run = assign_quadrants(
        units,
        experiment_id,
        alpha_bps=experiment.alpha_bps,
        mde_bps=experiment.mde_bps,
        folds=folds,
        resamples=resamples,
        seed=seed,
    )
    assignments = run.assignments

    population = load_population(session, experiment_id)
    arms = {row.risk_id: (row.is_treatment, row.recovered) for row in population.rows}
    amounts = {row.risk_id: row.recovered_amount for row in population.rows}

    ranked = rank(arms, [assignment.uplift for assignment in assignments])
    qini = qini_evaluate(ranked)

    matrix = confusion_matrix(session, experiment_id, assignments)

    return UpliftReport(
        model_version=MODEL_VERSION,
        folds=folds,
        alpha_bps=experiment.alpha_bps,
        mde_bps=experiment.mde_bps,
        resamples=resamples,
        seed=seed,
        n_scored=len(assignments),
        qini=qini,
        top_capture=qini.top,
        top_amount_capture=amount_capture(ranked, amounts),
        quadrant_counts=run.counts,
        rule_counts=dict(sorted(run.rule_counts.items())),
        gray_zone=_gray_zone_breakdown(assignments),
        ladder=_ladder_usage(assignments),
        harm=_harm_summary(assignments, run.thresholds),
        thresholds=run.thresholds,
        confusion=matrix,
        acceptance=acceptance_criteria(matrix),
    )


# -- the report -----------------------------------------------------------


def _harm_effect(
    treatment: ArmSample,
    holdout: ArmSample,
    *,
    alpha_bps: int,
    resamples: int,
) -> RateEffect:
    """The mandate-cancellation effect, estimated exactly like recovery.

    Harm is pre-registered as a first-class outcome, not a footnote. A treatment
    that recovers money while destroying mandates is not a success, and the only
    way to know is to measure it with the same machinery and the same interval.
    """
    return rate_effect(
        list(treatment.harm_mandate_cancelled),
        list(holdout.harm_mandate_cancelled),
        alpha_bps=alpha_bps,
        resamples=resamples,
    )


@dataclass(frozen=True, slots=True)
class AmountMix:
    """Incremental recovery split into *more payers* and *different payers*.

    The ledger says how much money the treatment caused. It does not say
    **why**, and the two available answers have different operational meanings:
    the treatment recovered more orders at the control arm's typical value, or
    it recovered a different mix of orders. A lift driven entirely by mix is a
    lift that may not repeat.

        rate_effect       = round(ate_bps x n_treat x mean_holdout / 10000)
        amount_mix_effect = incremental_recovered - rate_effect

    `rate_effect` prices the recovery-rate lift at the **control** arm's mean
    amount, so it answers "what would this many extra recoveries have been worth
    at the untreated average". Everything the ledger measured beyond that is
    attributed to mix. There is deliberately **no third interaction term**: with
    two components defined as a value and a residual, the parts sum to the whole
    by construction, and a third would have to be carved out of one of them by a
    convention nobody has agreed.

    **This is an accounting identity over values the estimator already
    produced.** It re-derives no rate, no amount, and no interval — `ate_bps`,
    `incremental_recovered` and `mean_holdout` all arrive already computed, and
    none of them is modified.

    Integer throughout, in minor units, using the same half-up rounding as the
    ledger and the Qini curve. The single rounding happens once, on the rate
    component; the residual absorbs it exactly, which is what keeps the
    invariant true rather than approximately true.
    """

    rate_effect: int
    amount_mix_effect: int
    incremental_recovered: int
    #: The inputs, carried so the figure can be re-derived by a reader.
    ate_bps: int
    n_treatment: int
    mean_holdout: int

    def __post_init__(self) -> None:
        total = self.rate_effect + self.amount_mix_effect
        if total != self.incremental_recovered:
            raise EvaluationError(
                f"mix decomposition does not sum to the ledger: "
                f"{self.rate_effect} + {self.amount_mix_effect} = {total}, "
                f"but incremental recovered is {self.incremental_recovered}"
            )

    @property
    def is_rate_driven(self) -> bool:
        """Whether more payers explains more of the lift than a changed mix.

        Compared on magnitude: either component may be negative, and a large
        negative mix effect is as much an explanation as a large positive one.
        """
        return abs(self.rate_effect) >= abs(self.amount_mix_effect)

    def as_dict(self) -> dict[str, object]:
        return {
            "rate_effect": self.rate_effect,
            "amount_mix_effect": self.amount_mix_effect,
            "incremental_recovered": self.incremental_recovered,
            "ate_bps": self.ate_bps,
            "n_treatment": self.n_treatment,
            "mean_holdout": self.mean_holdout,
            "is_rate_driven": self.is_rate_driven,
        }


def amount_mix(recovery: RateEffect, ledger: AmountEffect) -> AmountMix:
    """Split the ledger's incremental figure by rate and by mix.

    Reads `ate_bps` from the rate effect and `n_treatment`, `mean_holdout` and
    `incremental_recovered` from the ledger. Computes nothing else.
    """
    # `round_half_up` already halves away from zero for a negative numerator,
    # which a negative ATE produces. The denominator is the basis-point scale.
    rate_component = round_half_up(
        recovery.ate_bps * ledger.n_treatment * ledger.mean_holdout, BPS_SCALE
    )
    return AmountMix(
        rate_effect=rate_component,
        amount_mix_effect=ledger.incremental_recovered - rate_component,
        incremental_recovered=ledger.incremental_recovered,
        ate_bps=recovery.ate_bps,
        n_treatment=ledger.n_treatment,
        mean_holdout=ledger.mean_holdout,
    )


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Everything Day 4 can honestly say about one benchmark run."""

    experiment_id: uuid.UUID
    experiment_name: str
    hypothesis: str
    primary_metric: str
    locked_at: datetime | None
    started_at: datetime | None
    alpha_bps: int
    power_bps: int
    mde_bps: int
    planned_n_per_arm: int
    holdout_bps: int

    population: AnalysisPopulation
    recovery: RateEffect
    harm: RateEffect
    ledger: AmountEffect
    #: The ledger's incremental figure, split by rate and by mix. An accounting
    #: identity over `recovery` and `ledger`; it changes neither.
    mix: AmountMix
    per_protocol_recovery: RateEffect
    balance: BalanceReport
    truth: GroundTruth
    strata: tuple[StratumTruth, ...]

    required_n_per_arm: int
    detectable_mde_bps: int
    bootstrap_seed: int
    bootstrap_resamples: int
    #: Present only when the run asked for it. Cross-fitting is expensive, and a
    #: Day 4 caller that wants the ledger should not pay for a model it will not
    #: read.
    uplift: UpliftReport | None = None
    deferred: tuple[tuple[str, str], ...] = field(default=DEFERRED_SECTIONS)

    @property
    def achieved_n_per_arm(self) -> int:
        return min(self.population.itt.treatment.n, self.population.itt.holdout.n)

    @property
    def is_underpowered(self) -> bool:
        return is_underpowered(self.achieved_n_per_arm, self.planned_n_per_arm)

    @property
    def ate_error_bps(self) -> int:
        """Estimated minus known. The number the whole exercise exists to show."""
        return self.recovery.ate_bps - self.truth.true_ate_bps

    @property
    def interval_covers_the_truth(self) -> bool:
        """Reported as a fact, not applied as a pass mark. The pre-registration
        sets no acceptance threshold and this module does not invent one."""
        return self.recovery.interval.contains(self.truth.true_ate_bps)

    @property
    def credited_share_bps(self) -> int:
        return self.ledger.credited_share_bps

    def as_dict(self) -> dict[str, object]:
        return {
            "label": SYNTHETIC_LABEL,
            "experiment": {
                "id": str(self.experiment_id),
                "name": self.experiment_name,
                "hypothesis": self.hypothesis,
                "primary_metric": self.primary_metric,
                "locked_at": self.locked_at.isoformat() if self.locked_at else None,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "alpha_bps": self.alpha_bps,
                "power_bps": self.power_bps,
                "mde_bps": self.mde_bps,
                "planned_n_per_arm": self.planned_n_per_arm,
                "holdout_bps": self.holdout_bps,
            },
            "power": {
                "achieved_n_per_arm": self.achieved_n_per_arm,
                "planned_n_per_arm": self.planned_n_per_arm,
                "required_n_per_arm": self.required_n_per_arm,
                "detectable_mde_bps": self.detectable_mde_bps,
                "is_underpowered": self.is_underpowered,
                "ci_width_bps": self.recovery.interval.width,
            },
            "recovery": self.recovery.as_dict(),
            "harm": self.harm.as_dict(),
            "ledger": self.ledger.as_dict(),
            "mix": self.mix.as_dict(),
            "per_protocol": {
                "recovery": self.per_protocol_recovery.as_dict(),
                "non_compliance_bps": self.population.per_protocol.non_compliance_bps,
                "excluded_total": self.population.per_protocol.excluded_total,
            },
            "balance": self.balance.as_dict(),
            "ground_truth": self.truth.as_dict(),
            "ground_truth_by_stratum": [
                {
                    "label": stratum.label,
                    "n": stratum.n,
                    "true_ate_bps": stratum.true_ate_bps,
                    "true_harm_ate_bps": stratum.true_harm_ate_bps,
                }
                for stratum in self.strata
            ],
            "accuracy": {
                "estimated_ate_bps": self.recovery.ate_bps,
                "true_ate_bps": self.truth.true_ate_bps,
                "error_bps": self.ate_error_bps,
                "interval_covers_the_truth": self.interval_covers_the_truth,
            },
            "bootstrap": {
                "seed": self.bootstrap_seed,
                "resamples": self.bootstrap_resamples,
            },
            "uplift": self.uplift.as_dict() if self.uplift else None,
            "deferred": [{"section": name, "reason": why} for name, why in self.deferred],
            "limitations": list(LIMITATIONS),
        }


def build_report(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    include_uplift: bool = False,
) -> EvaluationReport:
    """Load, estimate, score, and assemble. Reads only; writes nothing.

    No `experiment_results` row is written. Three of that table's money columns
    are `NOT NULL` and cannot be computed without the economic inputs that do
    not exist yet, and filling them with zeros would put two non-measurements
    into columns documented as money.
    """
    experiment = session.get(Experiment, experiment_id)
    if experiment is None:
        raise EvaluationError(f"no experiment {experiment_id}")

    population = load_population(session, experiment_id)
    itt, per_protocol = population.itt, population.per_protocol

    recovery = rate_effect(
        list(itt.treatment.recovered),
        list(itt.holdout.recovered),
        alpha_bps=experiment.alpha_bps,
        resamples=resamples,
    )
    ledger = amount_effect(
        list(itt.treatment.amounts),
        list(itt.holdout.amounts),
        alpha_bps=experiment.alpha_bps,
        resamples=resamples,
    )
    harm = _harm_effect(
        itt.treatment,
        itt.holdout,
        alpha_bps=experiment.alpha_bps,
        resamples=resamples,
    )
    per_protocol_recovery = rate_effect(
        list(per_protocol.treatment.recovered),
        list(per_protocol.holdout.recovered),
        alpha_bps=experiment.alpha_bps,
        resamples=resamples,
    )

    achieved = min(itt.treatment.n, itt.holdout.n)
    baseline_bps = rate_bps(itt.holdout.recoveries, itt.holdout.n)

    uplift = (
        build_uplift_report(session, experiment_id, resamples=resamples) if include_uplift else None
    )

    return EvaluationReport(
        experiment_id=experiment.id,
        experiment_name=experiment.name,
        hypothesis=experiment.hypothesis,
        primary_metric=experiment.primary_metric,
        locked_at=experiment.locked_at,
        started_at=experiment.started_at,
        alpha_bps=experiment.alpha_bps,
        power_bps=experiment.power_bps,
        mde_bps=experiment.mde_bps,
        planned_n_per_arm=experiment.planned_n_per_arm,
        holdout_bps=experiment.holdout_bps,
        population=population,
        recovery=recovery,
        harm=harm,
        ledger=ledger,
        mix=amount_mix(recovery, ledger),
        per_protocol_recovery=per_protocol_recovery,
        balance=report_for_experiment(session, experiment_id),
        truth=ground_truth(session, experiment_id),
        strata=ground_truth_by_stratum(session, experiment_id),
        required_n_per_arm=required_n_per_arm(
            baseline_bps,
            experiment.mde_bps,
            alpha_bps=experiment.alpha_bps,
            power_bps=experiment.power_bps,
        ),
        detectable_mde_bps=mde_for_n(
            achieved,
            baseline_bps,
            alpha_bps=experiment.alpha_bps,
            power_bps=experiment.power_bps,
        ),
        bootstrap_seed=BOOTSTRAP_SEED,
        bootstrap_resamples=resamples,
        uplift=uplift,
    )


# -- rendering ------------------------------------------------------------


def _bps(value: int) -> str:
    """Basis points as a percentage, without forming a float."""
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    return f"{sign}{magnitude // 100}.{magnitude % 100:02d}%"


def _money(minor: int) -> str:
    """Minor units as rupees, without forming a float."""
    sign = "-" if minor < 0 else ""
    magnitude = abs(minor)
    return f"{sign}Rs {magnitude // 100:,}.{magnitude % 100:02d}"


def _p_value(micros: int) -> str:
    if micros == 0:
        return "< 0.000001"
    return f"{micros // 1_000_000}.{micros % 1_000_000:06d}"


def _interval_bps(low: int, high: int) -> str:
    return f"[{_bps(low)}, {_bps(high)}]"


def _optional_bps(value: int | None) -> str:
    """`None` is printed as undefined, never as zero.

    The distinction matters: a Qini coefficient of zero means the ranking did no
    better than chance, while an undefined one means there was no incremental
    recovery to apportion at all. Printing both as `0.00%` would merge a
    measurement with the absence of one.
    """
    return "undefined" if value is None else _bps(value)


def _write_uplift(write: Callable[[str], None], uplift: UpliftReport) -> None:
    """Sections 8a-8h: everything the uplift model measured."""
    write("## 8a. Uplift model")
    write("")
    write("| Quantity | Value |")
    write("|---|---|")
    write(f"| Model version | `{uplift.model_version}` |")
    write(f"| Cross-fitting folds | {uplift.folds} |")
    write(f"| Units scored | {uplift.n_scored:,} |")
    write(f"| alpha | {_bps(uplift.alpha_bps)} |")
    write(f"| MDE for cell qualification | {_bps(uplift.mde_bps)} |")
    write(f"| Bootstrap seed / resamples | `{uplift.seed}` / {uplift.resamples:,} |")
    write("")

    write("## 8b. Qini — does the ranking beat chance?")
    write("")
    curve = uplift.qini
    write("| Quantity | Value |")
    write("|---|---|")
    write(f"| Qini coefficient | {_optional_bps(curve.coefficient_bps)} |")
    write(f"| Q(N) — total incremental recoveries | {curve.curve.total:,} |")
    write(f"| Beats random | {'yes' if curve.beats_random else 'no'} |")
    write(f"| N treated / holdout | {curve.curve.n_treated:,} / {curve.curve.n_holdout:,} |")
    write("")
    write(
        "Positive means the ranking captured more incremental recovery than a random "
        "order; negative means it did worse. Undefined means `Q(N) == 0` and there was "
        "nothing to apportion."
    )
    write("")

    write(f"### Top {_bps(uplift.top_capture.share_bps)} capture")
    write("")
    write("| Weighting | k | Captured | Total | Share |")
    write("|---|---|---|---|---|")
    count = uplift.top_capture
    write(
        f"| By unit count | {count.k:,} | {count.qini_at_k:,} | {count.total:,} | "
        f"{_optional_bps(count.capture_bps)} |"
    )
    amount = uplift.top_amount_capture
    write(
        f"| By recovered amount | {amount.k:,} | {_money(amount.qini_at_k)} | "
        f"{_money(amount.total)} | {_optional_bps(amount.capture_bps)} |"
    )
    write("")
    write(
        "The two can disagree. A ranking that promotes many cheap recoveries scores "
        "well by count and poorly by money, and only the second is revenue."
    )
    write("")

    write("## 8c. Quadrant counts")
    write("")
    write("| Quadrant | N | Share |")
    write("|---|---|---|")
    total = max(1, uplift.n_scored)
    for label in QUADRANT_ORDER:
        n = uplift.quadrant_counts.get(label, 0)
        write(f"| `{label}` | {n:,} | {_bps(n * 10_000 // total)} |")
    write("")
    empty = uplift.confusion.empty_quadrants
    if empty:
        write(
            f"**Empty quadrants:** {', '.join(f'`{label}`' for label in empty)}. "
            "Listed rather than dropped — a quadrant nothing landed in was still "
            "looked for, and a reader must be able to tell that apart from one that "
            "was never evaluated."
        )
        write("")

    write("### By rule")
    write("")
    write("| Rule | N |")
    write("|---|---|")
    for rule, n in uplift.rule_counts.items():
        write(f"| `{rule}` | {n:,} |")
    write("")

    write("## 8d. Gray Zone, by reason")
    write("")
    write(
        "Two different situations produce the same label: a cell that never "
        "qualified, and a qualified cell whose result matched no rule. They are "
        "counted apart because they mean different things."
    )
    write("")
    write(f"Total Gray Zone: **{uplift.gray_zone.total:,}**")
    write("")
    write("| Quadrant rule | N |")
    write("|---|---|")
    for rule, n in uplift.gray_zone.by_rule.items():
        write(f"| `{rule}` | {n:,} |")
    write("")
    write("| Cell qualification reason | N |")
    write("|---|---|")
    for reason, n in uplift.gray_zone.by_reason.items():
        write(f"| `{reason}` | {n:,} |")
    write("")

    write("## 8e. Ladder level and cell usage")
    write("")
    write("| Level | N |")
    write("|---|---|")
    for level, n in uplift.ladder.by_level.items():
        write(f"| `{level}` | {n:,} |")
    write("")
    write(
        f"Distinct cells scored: **{uplift.ladder.distinct_cells}**. "
        f"Global fallbacks: **{uplift.ladder.global_fallbacks:,}**."
    )
    write("")
    write("| Cell | N |")
    write("|---|---|")
    for cell, n in uplift.ladder.cells:
        write(f"| `{cell}` | {n:,} |")
    write("")

    write("## 8f. Harm uplift")
    write("")
    write(
        "Computed in memory to decide sleeping-dog labels, then discarded — there is "
        "no column for it, by design. This report is the only place it is visible."
    )
    write("")
    harm = uplift.harm
    write("| Quantity | Value |")
    write("|---|---|")
    write(f"| Units | {harm.n:,} |")
    write(f"| Minimum | {_bps(harm.min_bps)} |")
    write(f"| Mean | {_bps(harm.mean_bps)} |")
    write(f"| Maximum | {_bps(harm.max_bps)} |")
    write(f"| Positive harm uplift | {harm.positive:,} |")
    write(f"| Above their fold's threshold | {harm.above_fold_threshold:,} |")
    write("")

    write("## 8g. Fold-local thresholds")
    write("")
    write(
        "Each fold's boundaries come from the four folds it trained on, and apply only "
        "to the fold it held out. Derived from the whole population instead, a unit's "
        "own outcome could move the boundary it is then judged against."
    )
    write("")
    write("| Fold | Self-recovery ceiling | Low tertile | High tertile | Harm threshold |")
    write("|---|---|---|---|---|")
    for threshold in uplift.thresholds:
        write(
            f"| {threshold.fold} | {_bps(threshold.self_recovery_ceiling_bps)} | "
            f"{_bps(threshold.low_tertile_bps)} | {_bps(threshold.high_tertile_bps)} | "
            f"{_bps(threshold.harm_threshold_bps)} |"
        )
    write("")

    write("## 8h. Quadrant confusion matrix against planted strata")
    write("")
    write(
        "**Only possible because the data is synthetic.** `truth_segment` is read here "
        "and nowhere else in the application. Every label being counted was produced by "
        "a model that saw only observed features; this comparison happens strictly "
        "afterwards and never feeds back."
    )
    write("")
    header = " | ".join(f"`{label}`" for label in uplift.confusion.quadrants)
    write(f"| Planted stratum | N | {header} | Modal | Mean uplift |")
    write("|---|---|" + "---|" * (len(uplift.confusion.quadrants) + 2))
    for segment in uplift.confusion.strata:
        cells = " | ".join(f"{segment.counts.get(label, 0):,}" for label in QUADRANT_ORDER)
        write(
            f"| `{segment.label}` | {segment.n:,} | {cells} | `{segment.modal_quadrant}` | "
            f"{_bps(segment.mean_uplift_bps)} |"
        )
    write("")

    write("### Acceptance criterion")
    write("")
    write("| Clause | Expected | Observed | Result |")
    write("|---|---|---|---|")
    for criterion in uplift.acceptance:
        mark = "**PASS**" if criterion.passed else "**FAIL**"
        write(f"| {criterion.name} | {criterion.expected} | {criterion.observed} | {mark} |")
    write("")
    write(f"Overall: **{'ACCEPTED' if uplift.accepted else 'NOT ACCEPTED'}**.")
    write("")
    write(
        "The self-recovering stratum is required not to be Persuadable, rather than to "
        "carry one specific label. The decision that matters is *do not spend money "
        "here*, which Gray Zone and Sure Thing both produce."
    )
    write("")

    write("### Limitations of the uplift model")
    write("")
    write("These qualify every number in sections 8a-8h.")
    write("")
    for limitation in UPLIFT_LIMITATIONS:
        write(f"- {limitation}")
    write("")


def render_markdown(report: EvaluationReport) -> str:
    """The report as Markdown. Every figure carries an interval or a count."""
    lines: list[str] = []
    write = lines.append

    write(f"# {SYNTHETIC_LABEL}")
    write("")
    write(
        "Every number below comes from a generated population with planted effects. "
        "None of it is evidence about real customers."
    )
    write("")

    write("## 1. Experiment")
    write("")
    write(f"- **Name:** {report.experiment_name}")
    write(f"- **Id:** `{report.experiment_id}`")
    write(f"- **Hypothesis:** {report.hypothesis}")
    write(f"- **Primary metric:** `{report.primary_metric}`")
    write(f"- **Locked at:** {report.locked_at.isoformat() if report.locked_at else 'not locked'}")
    write(
        f"- **Started at:** {report.started_at.isoformat() if report.started_at else 'not started'}"
    )
    write(f"- **Holdout:** {_bps(report.holdout_bps)} of enrolled units")
    write("")

    write("## 2. Power")
    write("")
    write("| Quantity | Value |")
    write("|---|---|")
    write(f"| Achieved N per arm | {report.achieved_n_per_arm:,} |")
    write(f"| Planned N per arm | {report.planned_n_per_arm:,} |")
    write(f"| Required N per arm (exact formula) | {report.required_n_per_arm:,} |")
    write(f"| alpha | {_bps(report.alpha_bps)} |")
    write(f"| Power | {_bps(report.power_bps)} |")
    write(f"| Pre-registered MDE | {_bps(report.mde_bps)} |")
    write(f"| Detectable effect at achieved N | {_bps(report.detectable_mde_bps)} |")
    write(f"| Achieved CI width | {_bps(report.recovery.interval.width)} |")
    write(f"| Underpowered | {'**YES**' if report.is_underpowered else 'no'} |")
    write("")
    if report.is_underpowered:
        write("> **INTERIM — UNDERPOWERED.** Achieved N is below the pre-registered plan.")
        write("")

    write("## 3. Covariate balance")
    write("")
    verdict = "BALANCED" if report.balance.is_balanced else "IMBALANCE FLAGGED"
    write(f"Flagged when `|SMD| > 0.10`. Verdict: **{verdict}**.")
    write("")
    write("| Covariate | Kind | Worst SMD | Flagged |")
    write("|---|---|---|---|")
    for covariate in report.balance.covariates:
        worst = covariate.worst_smd_bps
        shown = "undefined" if worst is None else _bps(worst)
        flag = "yes" if covariate.flagged else "no"
        write(f"| `{covariate.name}` | {covariate.kind} | {shown} | {flag} |")
    write("")

    write("## 4. The three headline numbers")
    write("")
    write("| Figure | Amount |")
    write("|---|---|")
    write(f"| Gross recovered (treated arm) | {_money(report.ledger.gross_recovered)} |")
    write(
        f"| **Incremental recovered** | {_money(report.ledger.incremental_recovered)} "
        f"CI [{_money(report.ledger.interval.low)}, {_money(report.ledger.interval.high)}] |"
    )
    write(f"| **Credited-not-earned** | {_money(report.ledger.credited_not_earned)} |")
    write(f"| Share of gross never caused | {_bps(report.credited_share_bps)} |")
    write("")
    write(
        "Gross is what a recovery dashboard reports. Credited-not-earned is the part "
        "of it that would have arrived anyway."
    )
    write("")

    write("### Why the incremental figure moved")
    write("")
    write(
        "The lift split two ways: recoveries the treatment caused, priced at the "
        "holdout's average order, and everything else — a different mix of orders "
        "recovered. A lift driven mostly by mix is a lift that may not repeat."
    )
    write("")
    write("| Component | Amount |")
    write("|---|---|")
    write(
        f"| Rate effect — more payers at the holdout average | {_money(report.mix.rate_effect)} |"
    )
    write(f"| Mix effect — a different set of orders | {_money(report.mix.amount_mix_effect)} |")
    write(f"| **Incremental recovered** | **{_money(report.mix.incremental_recovered)}** |")
    write("")
    driver = "the recovery-rate lift" if report.mix.is_rate_driven else "a changed order mix"
    write(
        f"Most of the movement is explained by {driver}. "
        f"Computed as `round({_bps(report.mix.ate_bps)} x "
        f"{report.mix.n_treatment:,} x {_money(report.mix.mean_holdout)})`, with the "
        "remainder taken as mix — the two sum to the incremental figure exactly, by "
        "construction rather than by coincidence."
    )
    write("")

    write("## 5. Primary metric — recovery rate (ITT)")
    write("")
    write("| Quantity | Value |")
    write("|---|---|")
    write(
        f"| Treated | {_bps(report.recovery.rate_treatment_bps)} "
        f"({report.recovery.hits_treatment:,} of {report.recovery.n_treatment:,}) |"
    )
    write(
        f"| Holdout | {_bps(report.recovery.rate_holdout_bps)} "
        f"({report.recovery.hits_holdout:,} of {report.recovery.n_holdout:,}) |"
    )
    write(f"| **ATE** | **{_bps(report.recovery.ate_bps)}** |")
    write(
        f"| 95% CI | {_interval_bps(report.recovery.interval.low, report.recovery.interval.high)} |"
    )
    write(f"| p-value | {_p_value(report.recovery.p_value_micros)} |")
    write("")

    write("## 6. Harm — mandate cancellation")
    write("")
    write("Pre-registered as a first-class outcome, not a footnote.")
    write("")
    write("| Quantity | Value |")
    write("|---|---|")
    write(f"| Treated | {_bps(report.harm.rate_treatment_bps)} |")
    write(f"| Holdout | {_bps(report.harm.rate_holdout_bps)} |")
    write(f"| **Harm ATE** | **{_bps(report.harm.ate_bps)}** |")
    write(f"| 95% CI | {_interval_bps(report.harm.interval.low, report.harm.interval.high)} |")
    write("")

    write("## 7. ITT and per-protocol side by side")
    write("")
    write("| Analysis | N treated | N holdout | ATE | 95% CI |")
    write("|---|---|---|---|---|")
    write(
        f"| ITT (primary) | {report.recovery.n_treatment:,} | {report.recovery.n_holdout:,} | "
        f"{_bps(report.recovery.ate_bps)} | "
        f"{_interval_bps(report.recovery.interval.low, report.recovery.interval.high)} |"
    )
    per_protocol = report.per_protocol_recovery
    write(
        f"| Per-protocol | {per_protocol.n_treatment:,} | {per_protocol.n_holdout:,} | "
        f"{_bps(per_protocol.ate_bps)} | "
        f"{_interval_bps(per_protocol.interval.low, per_protocol.interval.high)} |"
    )
    write("")
    write(
        f"Non-compliance: {_bps(report.population.per_protocol.non_compliance_bps)} "
        f"({report.population.per_protocol.excluded_total:,} units excluded from per-protocol)."
    )
    write("")

    write("## 8. Estimate against the known effect")
    write("")
    write(
        "**Only possible because the data is synthetic.** The true effect was written "
        "by hand; no production system can produce this section."
    )
    write("")
    write("| Quantity | Value |")
    write("|---|---|")
    write(f"| Estimated ATE | {_bps(report.recovery.ate_bps)} |")
    write(f"| True ATE | {_bps(report.truth.true_ate_bps)} |")
    write(f"| Error | {_bps(report.ate_error_bps)} |")
    covers = "yes" if report.interval_covers_the_truth else "no"
    write(f"| Interval contains the true value | {covers} |")
    write(f"| True harm ATE | {_bps(report.truth.true_harm_ate_bps)} |")
    write(f"| Self-recovery share (true) | {_bps(report.truth.self_recovery_share_bps)} |")
    write("")
    write("### Known effect by planted stratum")
    write("")
    write("| Stratum | N | True ATE | True harm ATE |")
    write("|---|---|---|---|")
    for stratum in report.strata:
        write(
            f"| `{stratum.label}` | {stratum.n:,} | {_bps(stratum.true_ate_bps)} | "
            f"{_bps(stratum.true_harm_ate_bps)} |"
        )
    write("")

    if report.uplift is not None:
        _write_uplift(write, report.uplift)

    write("## 9. Reproduction")
    write("")
    write(f"- Bootstrap seed: `{report.bootstrap_seed}`")
    write(f"- Bootstrap resamples: {report.bootstrap_resamples:,}")
    write(f"- Percentile method, resampled within arm, alpha {_bps(report.alpha_bps)}")
    write("")

    write("## 10. Deferred sections")
    write("")
    write("Listed rather than omitted. A gap a reader can see is a gap.")
    write("")
    for name, why in report.deferred:
        write(f"- **{name}** — {why}.")
    write("")

    write("## 11. Limitations")
    write("")
    for limitation in LIMITATIONS:
        write(f"- {limitation}")
    write("")

    return "\n".join(lines)


def render_json(report: EvaluationReport) -> dict[str, object]:
    """The same content as plain data."""
    return report.as_dict()


def sections_of(markdown: str) -> Sequence[str]:
    """Section headings, for tests and for a table of contents."""
    return [line[3:] for line in markdown.splitlines() if line.startswith("## ")]


__all__ = [
    "DEFERRED_SECTIONS",
    "LIMITATIONS",
    "QUADRANT_ORDER",
    "SEGMENT_PERSUADABLE",
    "SEGMENT_SELF_RECOVERING",
    "SEGMENT_SLEEPING_DOG",
    "SYNTHETIC_LABEL",
    "UPLIFT_LIMITATIONS",
    "BPS_SCALE",
    "AcceptanceCriterion",
    "AmountCapture",
    "AmountMix",
    "ConfusionMatrix",
    "EvaluationError",
    "EvaluationReport",
    "GrayZoneBreakdown",
    "GroundTruth",
    "HarmUpliftSummary",
    "LadderUsage",
    "SegmentRow",
    "StratumTruth",
    "UpliftReport",
    "acceptance_criteria",
    "amount_mix",
    "amount_capture",
    "build_report",
    "build_uplift_report",
    "confusion_matrix",
    "ground_truth",
    "ground_truth_by_stratum",
    "render_json",
    "render_markdown",
    "sections_of",
]
