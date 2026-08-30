"""Effect estimation, checked against hand arithmetic and known answers.

The bootstrap is checked two ways: that it is reproducible to the last unit
from its seed, and that it covers a known effect at roughly the advertised
rate when the data are drawn from a distribution whose answer we chose.

Floats appear here as an independent reference. They must never appear in the
implementation.
"""

from __future__ import annotations

import ast
import inspect
import math
import pathlib
import random
from statistics import NormalDist

import pytest

from app.causal.estimators import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    P_VALUE_SCALE,
    EstimatorError,
    Interval,
    amount_effect,
    ate_bps,
    benjamini_hochberg,
    bootstrap_interval,
    credited_not_earned,
    gross_recovered,
    incremental_recovered,
    mean_minor,
    rate_bps,
    rate_effect,
    two_proportion_p_micros,
    two_proportion_z,
)
from app.causal.normal import SCALE

ALPHA = 500  # the pre-registered alpha, in basis points
FAST = 400  # resamples for tests that are not about the bootstrap itself


def arm(hits: int, total: int) -> list[int]:
    """`hits` ones followed by zeros, since only the sum matters."""
    return [1] * hits + [0] * (total - hits)


def float_z(hits_t: int, n_t: int, hits_h: int, n_h: int) -> float:
    """The pooled two-proportion statistic in floating point, as a reference."""
    p_t, p_h = hits_t / n_t, hits_h / n_h
    pooled = (hits_t + hits_h) / (n_t + n_h)
    variance = pooled * (1 - pooled) * (1 / n_t + 1 / n_h)
    return (p_t - p_h) / math.sqrt(variance)


class TestRates:
    def test_a_hand_computable_rate(self) -> None:
        assert rate_bps(45, 100) == 4_500
        assert rate_bps(1, 3) == 3_333

    def test_it_rounds_half_up(self) -> None:
        assert rate_bps(1, 8) == 1_250
        assert rate_bps(1, 16) == 625
        assert rate_bps(1, 32) == 313  # 312.5 exactly, rounds up

    def test_the_ends(self) -> None:
        assert rate_bps(0, 100) == 0
        assert rate_bps(100, 100) == 10_000

    def test_an_empty_arm_is_a_zero_rate_not_a_crash(self) -> None:
        assert rate_bps(0, 0) == 0

    def test_impossible_counts_are_rejected(self) -> None:
        with pytest.raises(EstimatorError, match="exceeds total"):
            rate_bps(11, 10)
        with pytest.raises(EstimatorError, match="non-negative"):
            rate_bps(-1, 10)

    def test_the_ate_is_a_difference_of_rates(self) -> None:
        assert ate_bps(45, 100, 35, 100) == 1_000

    def test_a_worse_treatment_gives_a_negative_ate(self) -> None:
        """A negative effect is a result, not an error. One planted stratum is
        expected to produce exactly this."""
        assert ate_bps(30, 100, 40, 100) == -1_000

    def test_it_handles_unequal_arms(self) -> None:
        assert ate_bps(90, 200, 35, 100) == 4_500 - 3_500


class TestMeans:
    def test_zeros_are_included(self) -> None:
        """Dropping non-recoveries would measure how much payers paid."""
        assert mean_minor([100, 0, 0, 0]) == 25

    def test_it_rounds_half_up(self) -> None:
        assert mean_minor([1, 2]) == 2  # 1.5
        assert mean_minor([1, 1, 2]) == 1  # 1.33

    def test_an_empty_arm_is_rejected(self) -> None:
        with pytest.raises(EstimatorError, match="empty arm"):
            mean_minor([])


class TestTwoProportionTest:
    def test_it_matches_the_float_reference(self) -> None:
        for hits_t, n_t, hits_h, n_h in (
            (45, 100, 35, 100),
            (450, 1000, 350, 1000),
            (12, 40, 30, 90),
            (3, 500, 1, 500),
            (99, 100, 1, 100),
        ):
            expected = float_z(hits_t, n_t, hits_h, n_h)
            actual = two_proportion_z(hits_t, n_t, hits_h, n_h) / SCALE
            assert abs(actual - expected) < 1e-9, (hits_t, n_t, hits_h, n_h)

    def test_it_is_zero_when_the_rates_match(self) -> None:
        assert two_proportion_z(50, 100, 100, 200) == 0

    def test_the_sign_follows_the_difference(self) -> None:
        assert two_proportion_z(60, 100, 40, 100) > 0
        assert two_proportion_z(40, 100, 60, 100) < 0

    def test_swapping_the_arms_negates_it_exactly(self) -> None:
        assert two_proportion_z(37, 111, 52, 143) == -two_proportion_z(52, 143, 37, 111)

    def test_nobody_recovered_is_zero_not_undefined(self) -> None:
        """Pooled variance is zero, but so is the difference — every unit in
        both arms holds the same value, so there is nothing to test."""
        assert two_proportion_z(0, 100, 0, 100) == 0
        assert two_proportion_p_micros(0, 100, 0, 100) == P_VALUE_SCALE

    def test_everybody_recovered_is_zero_too(self) -> None:
        assert two_proportion_z(100, 100, 80, 80) == 0

    def test_the_p_value_matches_the_reference(self) -> None:
        reference = NormalDist()
        for hits_t, n_t, hits_h, n_h in ((45, 100, 35, 100), (12, 40, 30, 90), (3, 500, 1, 500)):
            z = abs(float_z(hits_t, n_t, hits_h, n_h))
            expected = 2 * (1 - reference.cdf(z)) * P_VALUE_SCALE
            actual = two_proportion_p_micros(hits_t, n_t, hits_h, n_h)
            assert abs(actual - expected) <= 1, (hits_t, n_t, hits_h, n_h)

    def test_a_bigger_sample_makes_the_same_effect_more_significant(self) -> None:
        small = two_proportion_p_micros(45, 100, 35, 100)
        large = two_proportion_p_micros(450, 1000, 350, 1000)
        assert large < small

    def test_it_never_leaves_the_stored_range(self) -> None:
        for hits_t, n_t, hits_h, n_h in ((0, 10, 10, 10), (5, 10, 5, 10), (9999, 10000, 1, 10000)):
            assert 0 <= two_proportion_p_micros(hits_t, n_t, hits_h, n_h) <= P_VALUE_SCALE

    def test_an_empty_arm_is_rejected(self) -> None:
        with pytest.raises(EstimatorError, match="at least one unit"):
            two_proportion_z(0, 0, 5, 10)


class TestBootstrapMechanics:
    def test_the_pre_registered_resample_count(self) -> None:
        assert BOOTSTRAP_RESAMPLES == 10_000

    def test_the_seed_is_fixed_not_drawn(self) -> None:
        """A seed drawn at runtime would make every interval unreproducible."""
        assert isinstance(BOOTSTRAP_SEED, int)
        assert BOOTSTRAP_SEED == 20_260_830

    def test_the_same_seed_gives_the_identical_interval(self) -> None:
        treatment, holdout = arm(45, 100), arm(35, 100)
        first = rate_effect(treatment, holdout, alpha_bps=ALPHA, resamples=FAST)
        second = rate_effect(treatment, holdout, alpha_bps=ALPHA, resamples=FAST)
        assert first.interval == second.interval

    def test_a_different_seed_gives_a_different_interval(self) -> None:
        treatment, holdout = arm(45, 100), arm(35, 100)
        first = rate_effect(treatment, holdout, alpha_bps=ALPHA, resamples=FAST, seed=1)
        second = rate_effect(treatment, holdout, alpha_bps=ALPHA, resamples=FAST, seed=2)
        assert (first.interval.low, first.interval.high) != (
            second.interval.low,
            second.interval.high,
        )

    def test_the_interval_reports_the_seed_that_made_it(self) -> None:
        effect = rate_effect(arm(45, 100), arm(35, 100), alpha_bps=ALPHA, resamples=FAST)
        assert effect.interval.seed == BOOTSTRAP_SEED
        assert effect.interval.resamples == FAST
        assert effect.interval.alpha_bps == ALPHA

    def test_row_order_within_an_arm_does_not_matter(self) -> None:
        """A resample draws indices, so without canonicalising the arm first a
        seeded run over the same data in a different order would pick different
        values and shift the interval. The interval must be a function of the
        data, not of the order the loader happened to return rows in."""
        treatment, holdout = arm(45, 100), arm(35, 100)
        shuffled_t, shuffled_h = list(treatment), list(holdout)
        random.Random(3).shuffle(shuffled_t)
        random.Random(4).shuffle(shuffled_h)

        plain = rate_effect(treatment, holdout, alpha_bps=ALPHA, resamples=FAST)
        mixed = rate_effect(shuffled_t, shuffled_h, alpha_bps=ALPHA, resamples=FAST)
        assert plain.interval == mixed.interval

    def test_the_interval_brackets_the_point_estimate(self) -> None:
        effect = rate_effect(arm(450, 1000), arm(350, 1000), alpha_bps=ALPHA, resamples=2_000)
        assert effect.interval.contains(effect.ate_bps)

    def test_a_tighter_alpha_widens_the_interval(self) -> None:
        treatment, holdout = arm(450, 1000), arm(350, 1000)
        wide = rate_effect(treatment, holdout, alpha_bps=100, resamples=2_000).interval
        narrow = rate_effect(treatment, holdout, alpha_bps=2_000, resamples=2_000).interval
        assert wide.width >= narrow.width

    def test_more_data_narrows_the_interval(self) -> None:
        small = rate_effect(arm(45, 100), arm(35, 100), alpha_bps=ALPHA, resamples=2_000)
        large = rate_effect(arm(4500, 10000), arm(3500, 10000), alpha_bps=ALPHA, resamples=2_000)
        assert large.interval.width < small.interval.width

    def test_identical_arms_produce_an_interval_around_zero(self) -> None:
        effect = rate_effect(arm(40, 100), arm(40, 100), alpha_bps=ALPHA, resamples=2_000)
        assert effect.ate_bps == 0
        assert effect.interval.contains_zero
        assert not effect.is_significant

    def test_a_large_real_effect_excludes_zero(self) -> None:
        effect = rate_effect(arm(4500, 10000), arm(3500, 10000), alpha_bps=ALPHA, resamples=2_000)
        assert effect.is_significant
        assert not effect.interval.contains_zero
        assert effect.interval.low > 0

    def test_too_few_resamples_for_the_alpha_are_refused(self) -> None:
        with pytest.raises(EstimatorError, match="tails would meet"):
            bootstrap_interval([1, 0], [1, 0], lambda a, b, c, d: a, alpha_bps=ALPHA, resamples=2)

    def test_an_empty_arm_is_refused(self) -> None:
        with pytest.raises(EstimatorError, match="at least one unit"):
            bootstrap_interval([], [1], lambda a, b, c, d: a, alpha_bps=ALPHA, resamples=FAST)

    def test_an_inverted_interval_cannot_be_constructed(self) -> None:
        with pytest.raises(EstimatorError, match="inverted"):
            Interval(low=10, high=5, alpha_bps=ALPHA, resamples=FAST, seed=1)


class TestBootstrapCoverage:
    def test_it_covers_a_known_effect_near_the_advertised_rate(self) -> None:
        """The real check on an interval: draw from a distribution whose answer
        we chose, and count how often the interval contains it. A 95% interval
        that covered 60% of the time would be decoration."""
        generator = random.Random(20260830)
        true_rate_treatment, true_rate_holdout = 45, 35
        true_ate_bps = 1_000
        trials, covered = 60, 0

        def draw(rate: int) -> list[int]:
            return [1 if generator.randrange(100) < rate else 0 for _ in range(400)]

        for _ in range(trials):
            treatment = draw(true_rate_treatment)
            holdout = draw(true_rate_holdout)
            effect = rate_effect(treatment, holdout, alpha_bps=ALPHA, resamples=600)
            if effect.interval.contains(true_ate_bps):
                covered += 1

        assert covered >= 48, f"covered {covered}/{trials}"


class TestTheLedger:
    def test_gross_is_the_treated_sum(self) -> None:
        assert gross_recovered([100, 0, 250, 0]) == 350

    def test_incremental_is_the_lift_times_the_treated_count(self) -> None:
        """means 250 and 100, difference 150, times n_t = 4 -> 600."""
        treatment = [1000, 0, 0, 0]
        holdout = [400, 0, 0, 0]
        assert incremental_recovered(treatment, holdout) == 600

    def test_credited_not_earned_is_the_remainder(self) -> None:
        assert credited_not_earned(1000, 600) == 400

    def test_the_three_numbers_are_consistent(self) -> None:
        treatment = [500, 0, 300, 0, 0]
        holdout = [500, 0, 0, 0, 0]
        effect = amount_effect(treatment, holdout, alpha_bps=ALPHA, resamples=FAST)
        assert effect.gross_recovered == 800
        assert effect.credited_not_earned == effect.gross_recovered - effect.incremental_recovered

    def test_a_holdout_that_recovers_as_much_leaves_nothing_incremental(self) -> None:
        """The headline claim collapses entirely when the untreated arm would
        have paid anyway. This is the whole point of the holdout."""
        values = [900, 0, 0, 700, 0]
        effect = amount_effect(values, list(values), alpha_bps=ALPHA, resamples=FAST)
        assert effect.gross_recovered == 1_600
        assert effect.incremental_recovered == 0
        assert effect.credited_not_earned == 1_600
        assert effect.credited_share_bps == 10_000

    def test_a_holdout_that_outperforms_gives_negative_incremental(self) -> None:
        """Acting made things worse. The schema deliberately allows this and so
        does the estimator; defining it away would hide the sleeping dog."""
        effect = amount_effect([100, 0, 0, 0], [900, 0, 0, 0], alpha_bps=ALPHA, resamples=FAST)
        assert effect.incremental_recovered < 0
        assert effect.credited_not_earned > effect.gross_recovered

    def test_credited_share_of_nothing_is_zero(self) -> None:
        effect = amount_effect([0, 0], [0, 0], alpha_bps=ALPHA, resamples=FAST)
        assert effect.gross_recovered == 0
        assert effect.credited_share_bps == 0

    def test_unequal_arms_are_handled(self) -> None:
        treatment = [1000] * 3 + [0] * 7
        holdout = [1000] + [0] * 4
        effect = amount_effect(treatment, holdout, alpha_bps=ALPHA, resamples=FAST)
        # mean_t = 300, mean_h = 200, lift 100, n_t = 10 -> 1000
        assert effect.mean_treatment == 300
        assert effect.mean_holdout == 200
        assert effect.incremental_recovered == 1_000

    def test_zeros_are_carried_into_the_means(self) -> None:
        effect = amount_effect([400, 0, 0, 0], [0, 0, 0, 0], alpha_bps=ALPHA, resamples=FAST)
        assert effect.mean_treatment == 100

    def test_the_incremental_interval_brackets_the_estimate(self) -> None:
        treatment = [1000 if index % 3 == 0 else 0 for index in range(300)]
        holdout = [1000 if index % 4 == 0 else 0 for index in range(300)]
        effect = amount_effect(treatment, holdout, alpha_bps=ALPHA, resamples=2_000)
        assert effect.interval.contains(effect.incremental_recovered)

    def test_negative_amounts_are_rejected(self) -> None:
        with pytest.raises(EstimatorError, match="cannot be negative"):
            amount_effect([-1, 0], [0, 0], alpha_bps=ALPHA, resamples=FAST)

    def test_an_empty_arm_is_rejected(self) -> None:
        with pytest.raises(EstimatorError, match="at least one unit"):
            incremental_recovered([100], [])


class TestEffectSerialisation:
    def test_the_rate_effect_carries_its_provenance(self) -> None:
        effect = rate_effect(arm(45, 100), arm(35, 100), alpha_bps=ALPHA, resamples=FAST)
        payload = effect.as_dict()
        assert payload["bootstrap_seed"] == BOOTSTRAP_SEED
        assert payload["bootstrap_resamples"] == FAST
        assert payload["ate_ci_low_bps"] <= payload["ate_bps"] <= payload["ate_ci_high_bps"]

    def test_neither_payload_carries_a_float(self) -> None:
        rate = rate_effect(arm(45, 100), arm(35, 100), alpha_bps=ALPHA, resamples=FAST)
        amount = amount_effect([100, 0], [50, 0], alpha_bps=ALPHA, resamples=FAST)
        for payload in (rate.as_dict(), amount.as_dict()):
            for value in payload.values():
                assert not isinstance(value, float), value

    def test_the_effects_are_frozen(self) -> None:
        effect = rate_effect(arm(45, 100), arm(35, 100), alpha_bps=ALPHA, resamples=FAST)
        with pytest.raises(AttributeError):
            effect.ate_bps = 0  # type: ignore[misc]

    def test_a_non_indicator_arm_is_rejected(self) -> None:
        with pytest.raises(EstimatorError, match="must be 0 or 1"):
            rate_effect([2, 0], [1, 0], alpha_bps=ALPHA, resamples=FAST)


class TestBenjaminiHochberg:
    def test_nothing_in_nothing_out(self) -> None:
        assert benjamini_hochberg([]) == ()

    def test_a_clearly_significant_family(self) -> None:
        assert benjamini_hochberg([1, 2, 3], q_bps=1_000) == (True, True, True)

    def test_a_clearly_null_family(self) -> None:
        assert benjamini_hochberg([900_000, 800_000, 950_000], q_bps=1_000) == (
            False,
            False,
            False,
        )

    def test_a_hand_worked_example(self) -> None:
        """m = 5, q = 0.10. Thresholds k*q/m are .02 .04 .06 .08 .10.
        Sorted p: .001 .008 .039 .041 .900. Ranks 1-4 all pass their own
        threshold and rank 5 fails, so the largest passing rank is 4 and the
        four smallest are rejected."""
        verdicts = benjamini_hochberg([1_000, 8_000, 39_000, 41_000, 900_000], q_bps=1_000)
        assert verdicts == (True, True, True, True, False)

    def test_the_step_up_carries_a_borderline_p_value(self) -> None:
        """BH rejects everything below the largest passing rank, even entries
        that would fail their own threshold. Naive per-rank testing would
        under-reject."""
        verdicts = benjamini_hochberg([10_000, 55_000, 60_000], q_bps=1_000)
        assert verdicts[2] is True
        assert all(verdicts)

    def test_verdicts_come_back_in_input_order(self) -> None:
        verdicts = benjamini_hochberg([900_000, 1_000, 800_000], q_bps=1_000)
        assert verdicts == (False, True, False)

    def test_it_is_more_permissive_than_bonferroni(self) -> None:
        p_values = [5_000] * 20
        assert all(benjamini_hochberg(p_values, q_bps=1_000))
        # Bonferroni at the same level would need p <= 0.10/20 = 0.005 exactly.

    def test_a_larger_q_rejects_at_least_as_much(self) -> None:
        p_values = [1_000, 30_000, 60_000, 400_000]
        strict = benjamini_hochberg(p_values, q_bps=500)
        loose = benjamini_hochberg(p_values, q_bps=2_000)
        assert sum(loose) >= sum(strict)

    def test_the_default_is_the_pre_registered_rate(self) -> None:
        p_values = [1_000, 39_000, 900_000]
        assert benjamini_hochberg(p_values) == benjamini_hochberg(p_values, q_bps=1_000)

    def test_out_of_range_inputs_are_rejected(self) -> None:
        with pytest.raises(EstimatorError, match="q_bps"):
            benjamini_hochberg([1_000], q_bps=0)
        with pytest.raises(EstimatorError, match="p-values must be within"):
            benjamini_hochberg([P_VALUE_SCALE + 1])


class TestPurity:
    """Estimators compute. They do not act, read a clock, or form a float."""

    @staticmethod
    def _tree() -> ast.Module:
        from app.causal import estimators as module

        return ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))

    @classmethod
    def _identifiers(cls) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(cls._tree()):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.ClassDef):
                found.add(node.name)
        return found

    @classmethod
    def _imports(cls) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(cls._tree()):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
        return found

    def test_there_is_no_true_division(self) -> None:
        for node in ast.walk(self._tree()):
            assert not isinstance(node, ast.Div), ast.dump(node)

    def test_no_float_constant_appears(self) -> None:
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), node.value

    def test_no_float_or_decimal_is_constructed(self) -> None:
        for banned in ("float", "Decimal", "sqrt", "exp", "log", "fsum"):
            assert banned not in self._identifiers(), banned

    def test_no_scientific_stack_dependency(self) -> None:
        for module in self._imports():
            for banned in ("numpy", "scipy", "pandas", "sklearn", "statsmodels"):
                assert banned not in module.lower(), module

    def test_it_imports_no_generator_module(self) -> None:
        for module in self._imports():
            assert not module.startswith("simulator"), module

    def test_it_never_names_ground_truth(self) -> None:
        for name in self._identifiers():
            assert not name.startswith("truth_"), name

    def test_it_reads_no_clock(self) -> None:
        for name in ("now", "utcnow", "today"):
            assert name not in self._identifiers(), name

    def test_it_touches_no_database(self) -> None:
        identifiers = self._identifiers()
        for banned in ("Session", "select", "execute", "commit", "add", "flush", "session"):
            assert banned not in identifiers, banned

    def test_it_creates_no_recovery_or_policy_concept(self) -> None:
        identifiers = self._identifiers()
        for banned in (
            "RecoveryCase",
            "RecoveryAction",
            "ExperimentResult",
            "approve",
            "approved",
            "policy_status",
            "execute_action",
            "recommend",
        ):
            assert banned not in identifiers, banned

    def test_it_invents_no_economic_input(self) -> None:
        """Gross margin and lifetime value do not exist in this codebase yet.
        Net incremental value waits for them rather than guessing."""
        identifiers = self._identifiers()
        for banned in ("gross_margin", "avg_customer_ltv", "harm_cost", "net_incremental_value"):
            assert banned not in identifiers, banned

    def test_the_randomness_is_seeded_from_the_module_constant(self) -> None:
        """`random.Random(seed)` with a fixed default — never `random.seed()`
        on the global generator, and never a runtime-drawn value."""
        source = pathlib.Path(inspect.getfile(__import__("app.causal.estimators", fromlist=["x"])))
        text = source.read_text(encoding="utf-8")
        assert "random.Random(seed)" in text
        assert "randbytes" not in text
        assert "urandom" not in text
        assert "SystemRandom" not in text
