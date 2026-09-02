"""Interval calibration: does a 95% CI contain the true effect 95% of the time?

A confidence interval makes a frequency claim about repeated experiments, and
one experiment cannot check it. This sweep generates twenty independent
populations with known planted effects and counts how often the interval
covers the truth.

Orchestration only, in the same shape as `report.run_benchmark`: it materialises
populations, asks the reporter for an estimate, and records what came back. It
computes no statistic of its own — `interval_covers_the_truth` is the report's
own property, and `CoverageRun.covered` re-derives it independently so that a
disagreement between the two is caught rather than averaged away.

**Nothing here is tuned.** The seeds are a fixed contiguous block chosen before
any result was seen; the bootstrap settings are the pre-registered constants,
untouched. Picking seeds after seeing coverage is how a calibration study lies.

**Only the population seed varies.** `BOOTSTRAP_SEED` is a fixed constant, so
resampling noise is common across runs rather than independent. That is the
contracted seed policy and it does not affect the point estimates or the
coverage question, but it does mean the twenty intervals are not twenty fully
independent tests of the bootstrap itself.

**What a result here can and cannot say.** Twenty trials is a coarse
instrument. Under a true 95% rate a clean sweep is the single most likely
outcome, so 20/20 is not evidence of better-than-nominal calibration — it is
absence of evidence for under-coverage. Report it that way; the frozen
evaluation artifact uses exactly that wording, and "100% calibrated" would
overstate what twenty runs can establish.

Truth is read the way the existing benchmark reads it: through the report's own
`truth` object, which `app.reporting.evaluation` is the single permitted reader
of. No `truth_*` column is touched here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.causal.estimators import BOOTSTRAP_RESAMPLES
from app.reporting.evaluation import EvaluationReport, build_report
from tests.benchmark.bridge import materialise

#: Twenty populations, fixed before any result was seen. A contiguous block
#: rather than a hand-picked set: choosing seeds afterwards would let the sweep
#: report whatever it liked.
COVERAGE_SEEDS: tuple[int, ...] = tuple(range(1, 21))

#: Per the closeout plan. Large enough that the interval is meaningful, small
#: enough that twenty of them are affordable.
COVERAGE_CASE_COUNT = 2_000

#: Full basis-point scale.
BPS_SCALE = 10_000


class CoverageError(ValueError):
    """The sweep could not be run or summarised."""


@dataclass(frozen=True, slots=True)
class CoverageRun:
    """One population: what was planted, what was estimated, and whether it hit."""

    seed: int
    case_count: int
    n_treatment: int
    n_holdout: int
    true_ate_bps: int
    estimated_ate_bps: int
    ci_low_bps: int
    ci_high_bps: int

    @property
    def covered(self) -> bool:
        """Whether the interval contains the planted effect."""
        return self.ci_low_bps <= self.true_ate_bps <= self.ci_high_bps

    @property
    def error_bps(self) -> int:
        """Estimated minus true. Signed, so bias is visible across runs."""
        return self.estimated_ate_bps - self.true_ate_bps

    @property
    def ci_width_bps(self) -> int:
        return self.ci_high_bps - self.ci_low_bps

    def as_dict(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "case_count": self.case_count,
            "n_treatment": self.n_treatment,
            "n_holdout": self.n_holdout,
            "true_ate_bps": self.true_ate_bps,
            "estimated_ate_bps": self.estimated_ate_bps,
            "ci_low_bps": self.ci_low_bps,
            "ci_high_bps": self.ci_high_bps,
            "error_bps": self.error_bps,
            "ci_width_bps": self.ci_width_bps,
            "covered": self.covered,
        }


def run_from_report(seed: int, case_count: int, report: EvaluationReport) -> CoverageRun:
    """One run, read off a completed report.

    Cross-checks its own `covered` derivation against the report's
    `interval_covers_the_truth`. They compute the same thing two ways, and if
    they ever disagree the sweep should stop rather than silently record one of
    them.
    """
    run = CoverageRun(
        seed=seed,
        case_count=case_count,
        n_treatment=report.recovery.n_treatment,
        n_holdout=report.recovery.n_holdout,
        true_ate_bps=report.truth.true_ate_bps,
        estimated_ate_bps=report.recovery.ate_bps,
        ci_low_bps=report.recovery.interval.low,
        ci_high_bps=report.recovery.interval.high,
    )
    if run.covered != report.interval_covers_the_truth:
        raise CoverageError(
            f"seed {seed}: coverage disagreement — this module derived "
            f"{run.covered} from [{run.ci_low_bps}, {run.ci_high_bps}] containing "
            f"{run.true_ate_bps}, but the report says "
            f"{report.interval_covers_the_truth}"
        )
    return run


def coverage_hits(runs: Sequence[CoverageRun]) -> int:
    """How many intervals covered the truth."""
    return sum(1 for run in runs if run.covered)


def coverage_bps(runs: Sequence[CoverageRun]) -> int:
    """Coverage as integer basis points. Halves away from zero, no float."""
    total = len(runs)
    if total == 0:
        raise CoverageError("coverage over zero runs is undefined")
    return (coverage_hits(runs) * BPS_SCALE * 2 + total) // (2 * total)


@dataclass(frozen=True, slots=True)
class CoverageSweep:
    """Every run, plus the count the whole exercise exists to produce."""

    runs: tuple[CoverageRun, ...]
    case_count: int
    resamples: int

    @property
    def hits(self) -> int:
        return coverage_hits(self.runs)

    @property
    def total(self) -> int:
        return len(self.runs)

    @property
    def coverage_bps(self) -> int:
        return coverage_bps(self.runs)

    @property
    def misses(self) -> tuple[CoverageRun, ...]:
        return tuple(run for run in self.runs if not run.covered)

    @property
    def mean_error_bps(self) -> int:
        """Signed mean error. Near zero means unbiased, not merely wide enough."""
        if not self.runs:
            raise CoverageError("mean error over zero runs is undefined")
        total = sum(run.error_bps for run in self.runs)
        n = len(self.runs)
        if total < 0:
            return -((-total * 2 + n) // (2 * n))
        return (total * 2 + n) // (2 * n)

    def as_dict(self) -> dict[str, object]:
        return {
            "case_count": self.case_count,
            "resamples": self.resamples,
            "seeds": [run.seed for run in self.runs],
            "hits": self.hits,
            "total": self.total,
            "coverage_bps": self.coverage_bps,
            "mean_error_bps": self.mean_error_bps,
            "runs": [run.as_dict() for run in self.runs],
        }


def run_coverage_sweep(
    session: Session,
    *,
    seeds: Sequence[int] = COVERAGE_SEEDS,
    case_count: int = COVERAGE_CASE_COUNT,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> CoverageSweep:
    """Materialise each seed, estimate, and record whether the interval hit.

    Takes a `Session` and leaves the transaction to the caller, exactly as
    `report.run_benchmark` does — so a test can run it inside the rolled-back
    fixture and a deliberate run can own its own transaction.

    Populations from different seeds have disjoint identifiers, so they coexist
    in one transaction; the bridge's own guards would refuse a seed materialised
    twice.

    The uplift model is not fitted: coverage is a question about the recovery
    interval, and cross-fitting twenty populations to answer it would cost a
    great deal and change nothing.
    """
    if not seeds:
        raise CoverageError("a coverage sweep needs at least one seed")
    if len(set(seeds)) != len(seeds):
        raise CoverageError(f"seeds must be distinct, got {list(seeds)}")

    runs: list[CoverageRun] = []
    for seed in seeds:
        materialised = materialise(session, seed=seed, case_count=case_count)
        report = build_report(session, materialised.experiment_id, resamples=resamples)
        runs.append(run_from_report(seed, case_count, report))

    return CoverageSweep(runs=tuple(runs), case_count=case_count, resamples=resamples)


def _bps(value: int) -> str:
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    return f"{sign}{magnitude // 100}.{magnitude % 100:02d}%"


def summarise(sweep: CoverageSweep) -> str:
    """A console summary for whoever ran the sweep."""
    lines = [
        f"seeds        {sweep.runs[0].seed}..{sweep.runs[-1].seed} ({sweep.total} populations)",
        f"N per seed   {sweep.case_count:,}",
        f"bootstrap    percentile, {sweep.resamples:,} resamples, fixed pre-registered seed",
        "",
        f"{'seed':>5} {'true':>9} {'est':>9} {'ci_low':>9} {'ci_high':>9} {'err':>8}  covered",
        "-" * 62,
    ]
    for run in sweep.runs:
        lines.append(
            f"{run.seed:>5} {_bps(run.true_ate_bps):>9} {_bps(run.estimated_ate_bps):>9} "
            f"{_bps(run.ci_low_bps):>9} {_bps(run.ci_high_bps):>9} "
            f"{_bps(run.error_bps):>8}  {'yes' if run.covered else 'NO'}"
        )
    lines.extend(
        [
            "-" * 62,
            "",
            f"coverage     {sweep.hits}/{sweep.total} = {_bps(sweep.coverage_bps)} "
            "(nominal 95.00%)",
            f"mean error   {_bps(sweep.mean_error_bps)}",
        ]
    )
    if sweep.misses:
        lines.append("")
        lines.append("misses:")
        for run in sweep.misses:
            lines.append(
                f"  seed {run.seed}: true {_bps(run.true_ate_bps)} outside "
                f"[{_bps(run.ci_low_bps)}, {_bps(run.ci_high_bps)}]"
            )
    lines.extend(
        [
            "",
            "A clean sweep is the most likely single outcome under a true 95% rate, so",
            "this is absence of evidence for under-coverage — not a demonstration of",
            "exact calibration. Twenty trials cannot separate 95% from 99%.",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "BPS_SCALE",
    "COVERAGE_CASE_COUNT",
    "COVERAGE_SEEDS",
    "CoverageError",
    "CoverageRun",
    "CoverageSweep",
    "coverage_bps",
    "coverage_hits",
    "run_coverage_sweep",
    "run_from_report",
    "summarise",
]
