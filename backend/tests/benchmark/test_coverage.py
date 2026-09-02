"""The CI coverage sweep: its shape, and the arithmetic that summarises it.

Split deliberately. The coverage *calculation* is pure and is tested against
hand-built runs with no database at all — that is the part a regression would
silently corrupt. The *sweep* is tested against a real database at a size small
enough for the fast suite; the full twenty-seed run at N=2,000 is a deliberate
exercise, not something every test session pays for.

Nothing here asserts a particular coverage number. A test that required 19/20
or 20/20 would be tuning the study to its conclusion, and the sweep exists to
measure whatever is true.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.causal.estimators import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED
from tests.benchmark.coverage import (
    COVERAGE_CASE_COUNT,
    COVERAGE_SEEDS,
    CoverageError,
    CoverageRun,
    CoverageSweep,
    coverage_bps,
    coverage_hits,
    run_coverage_sweep,
    summarise,
)

#: Small enough for the fast suite. The sweep's behaviour does not depend on it.
SMALL_CASE_COUNT = 120
SMALL_SEEDS = (1, 2, 3)
FAST_RESAMPLES = 60


def a_run(
    *,
    seed: int = 1,
    true_ate_bps: int = 1_000,
    estimated_ate_bps: int = 1_100,
    ci_low_bps: int = 800,
    ci_high_bps: int = 1_400,
) -> CoverageRun:
    return CoverageRun(
        seed=seed,
        case_count=2_000,
        n_treatment=1_000,
        n_holdout=1_000,
        true_ate_bps=true_ate_bps,
        estimated_ate_bps=estimated_ate_bps,
        ci_low_bps=ci_low_bps,
        ci_high_bps=ci_high_bps,
    )


# -- the pre-registered configuration -------------------------------------


class TestConfiguration:
    def test_exactly_twenty_distinct_seeds(self) -> None:
        assert len(COVERAGE_SEEDS) == 20
        assert len(set(COVERAGE_SEEDS)) == 20

    def test_the_seeds_are_a_fixed_contiguous_block(self) -> None:
        """Chosen before any result was seen. Hand-picking would be selection bias."""
        assert COVERAGE_SEEDS == tuple(range(1, 21))

    def test_the_case_count_is_two_thousand(self) -> None:
        assert COVERAGE_CASE_COUNT == 2_000

    def test_the_bootstrap_settings_are_untouched(self) -> None:
        """The sweep uses the pre-registered constants; it does not define its own."""
        assert BOOTSTRAP_RESAMPLES == 10_000
        assert BOOTSTRAP_SEED == 20_260_830


# -- the calculation, with no database ------------------------------------


class TestCoveredIsIntervalContainment:
    def test_truth_inside_the_interval_counts(self) -> None:
        assert a_run(true_ate_bps=1_000, ci_low_bps=800, ci_high_bps=1_400).covered

    def test_truth_below_the_interval_does_not(self) -> None:
        assert not a_run(true_ate_bps=700, ci_low_bps=800, ci_high_bps=1_400).covered

    def test_truth_above_the_interval_does_not(self) -> None:
        assert not a_run(true_ate_bps=1_500, ci_low_bps=800, ci_high_bps=1_400).covered

    @pytest.mark.parametrize("truth", [800, 1_400])
    def test_the_bounds_are_inclusive(self, truth: int) -> None:
        """A truth exactly on a bound is covered. The interval is closed."""
        assert a_run(true_ate_bps=truth, ci_low_bps=800, ci_high_bps=1_400).covered

    def test_error_is_signed(self) -> None:
        """Bias must stay visible; a magnitude would average it away."""
        assert a_run(true_ate_bps=1_000, estimated_ate_bps=900).error_bps == -100
        assert a_run(true_ate_bps=1_000, estimated_ate_bps=1_100).error_bps == 100


class TestCoverageArithmetic:
    def test_hits_counts_covered_runs(self) -> None:
        runs = [
            a_run(seed=1, true_ate_bps=1_000),
            a_run(seed=2, true_ate_bps=9_999),
            a_run(seed=3, true_ate_bps=1_200),
        ]
        assert coverage_hits(runs) == 2

    @pytest.mark.parametrize(
        ("hits", "total", "expected_bps"),
        [
            (20, 20, 10_000),
            (19, 20, 9_500),
            (18, 20, 9_000),
            (0, 20, 0),
            (1, 3, 3_333),
            (2, 3, 6_667),
        ],
    )
    def test_coverage_bps_is_exact_and_rounds_half_up(
        self, hits: int, total: int, expected_bps: int
    ) -> None:
        """Integer basis points, halves away from zero. No float anywhere."""
        runs = [a_run(seed=i, true_ate_bps=1_000) for i in range(hits)]
        runs += [a_run(seed=100 + i, true_ate_bps=9_999) for i in range(total - hits)]
        assert coverage_bps(runs) == expected_bps

    def test_coverage_over_no_runs_is_refused(self) -> None:
        """Undefined, not zero. Zero would read as total failure."""
        with pytest.raises(CoverageError, match="undefined"):
            coverage_bps([])

    def test_mean_error_is_signed_and_rounds_half_up(self) -> None:
        sweep = CoverageSweep(
            runs=(
                a_run(seed=1, true_ate_bps=1_000, estimated_ate_bps=900),
                a_run(seed=2, true_ate_bps=1_000, estimated_ate_bps=1_050),
            ),
            case_count=2_000,
            resamples=BOOTSTRAP_RESAMPLES,
        )
        # (-100 + 50) / 2 = -25
        assert sweep.mean_error_bps == -25

    def test_misses_lists_only_uncovered_runs(self) -> None:
        sweep = CoverageSweep(
            runs=(
                a_run(seed=1, true_ate_bps=1_000),
                a_run(seed=2, true_ate_bps=9_999),
            ),
            case_count=2_000,
            resamples=BOOTSTRAP_RESAMPLES,
        )
        assert sweep.hits == 1
        assert sweep.total == 2
        assert [r.seed for r in sweep.misses] == [2]


class TestSweepShape:
    def _sweep(self, hits: int, total: int) -> CoverageSweep:
        runs = [a_run(seed=i, true_ate_bps=1_000) for i in range(hits)]
        runs += [a_run(seed=100 + i, true_ate_bps=9_999) for i in range(total - hits)]
        return CoverageSweep(runs=tuple(runs), case_count=2_000, resamples=10_000)

    def test_as_dict_carries_every_run_and_the_headline(self) -> None:
        payload = self._sweep(19, 20).as_dict()
        assert payload["hits"] == 19
        assert payload["total"] == 20
        assert payload["coverage_bps"] == 9_500
        assert len(payload["runs"]) == 20  # type: ignore[arg-type]
        assert payload["case_count"] == 2_000
        assert payload["resamples"] == 10_000

    def test_every_run_carries_the_recorded_fields(self) -> None:
        row = self._sweep(1, 1).as_dict()["runs"][0]  # type: ignore[index]
        assert set(row) >= {
            "seed",
            "true_ate_bps",
            "estimated_ate_bps",
            "ci_low_bps",
            "ci_high_bps",
            "covered",
        }

    def test_the_summary_states_hits_over_total(self) -> None:
        text = summarise(self._sweep(19, 20))
        assert "19/20" in text
        assert "95.00%" in text

    def test_the_summary_refuses_to_claim_calibration(self) -> None:
        """A clean sweep must not be reported as proof of exact calibration.

        The disclaimer has to survive verbatim: this is the wording the frozen
        evaluation artifact uses, and weakening it here would let 20/20 be read
        as a stronger result than twenty trials can support.
        """
        text = summarise(self._sweep(20, 20))
        assert "20/20" in text
        assert "absence of evidence for under-coverage" in text
        assert "not a demonstration of" in text
        assert "cannot separate 95% from 99%" in text

        overclaims = ("100% calibrated", "perfectly calibrated", "proves calibration")
        assert not any(phrase in text.lower() for phrase in overclaims)

    def test_the_summary_names_misses(self) -> None:
        assert "misses:" in summarise(self._sweep(18, 20))


# -- against the real database --------------------------------------------


@pytest.mark.db
class TestSweepAgainstTheDatabase:
    def test_it_produces_one_run_per_seed(self, db_session: Session) -> None:
        sweep = run_coverage_sweep(
            db_session,
            seeds=SMALL_SEEDS,
            case_count=SMALL_CASE_COUNT,
            resamples=FAST_RESAMPLES,
        )
        assert [run.seed for run in sweep.runs] == list(SMALL_SEEDS)
        assert sweep.total == len(SMALL_SEEDS)

    def test_every_run_is_internally_consistent(self, db_session: Session) -> None:
        """Interval ordered, estimate inside it, arms summing to the population."""
        sweep = run_coverage_sweep(
            db_session,
            seeds=SMALL_SEEDS,
            case_count=SMALL_CASE_COUNT,
            resamples=FAST_RESAMPLES,
        )
        for run in sweep.runs:
            assert run.ci_low_bps <= run.ci_high_bps
            assert run.ci_low_bps <= run.estimated_ate_bps <= run.ci_high_bps
            assert run.n_treatment + run.n_holdout == SMALL_CASE_COUNT
            assert run.case_count == SMALL_CASE_COUNT

    def test_coverage_agrees_with_the_reports_own_verdict(self, db_session: Session) -> None:
        """`run_from_report` raises on disagreement, so reaching here proves it.

        The sweep derives `covered` from the interval itself; the report derives
        it via `interval_covers_the_truth`. Two paths, one answer.
        """
        sweep = run_coverage_sweep(
            db_session,
            seeds=SMALL_SEEDS,
            case_count=SMALL_CASE_COUNT,
            resamples=FAST_RESAMPLES,
        )
        assert sweep.hits == sum(1 for run in sweep.runs if run.covered)
        assert 0 <= sweep.coverage_bps <= 10_000

    def test_it_is_deterministic(self, db_session: Session) -> None:
        """The same seeds produce the same numbers. Rerun in a fresh transaction."""
        first = run_coverage_sweep(
            db_session, seeds=(7,), case_count=SMALL_CASE_COUNT, resamples=FAST_RESAMPLES
        )
        db_session.rollback()
        second = run_coverage_sweep(
            db_session, seeds=(7,), case_count=SMALL_CASE_COUNT, resamples=FAST_RESAMPLES
        )
        assert first.as_dict()["runs"] == second.as_dict()["runs"]

    def test_duplicate_seeds_are_refused(self, db_session: Session) -> None:
        """A repeated seed would double-count one population."""
        with pytest.raises(CoverageError, match="distinct"):
            run_coverage_sweep(
                db_session, seeds=(1, 1), case_count=SMALL_CASE_COUNT, resamples=FAST_RESAMPLES
            )

    def test_no_seeds_is_refused(self, db_session: Session) -> None:
        with pytest.raises(CoverageError, match="at least one seed"):
            run_coverage_sweep(
                db_session, seeds=(), case_count=SMALL_CASE_COUNT, resamples=FAST_RESAMPLES
            )
