"""Power, detectable effect, and the cost of a holdout.

The anchor is the pre-registered worked example: alpha 0.05, power 0.80,
baseline 0.35, MDE 10pp. Both the exact formula and the rule of thumb are
pinned, along with the reason they differ.

Floats appear here as an independent reference and nowhere in `app/causal/`.
"""

from __future__ import annotations

import ast
import inspect
import math
import pathlib
from statistics import NormalDist

import pytest

from app.causal.power import (
    BALANCED_HOLDOUT_BPS,
    DEFAULT_ALPHA_BPS,
    DEFAULT_POWER_BPS,
    PowerError,
    is_underpowered,
    mde_for_n,
    required_n_holdout,
    required_n_per_arm,
    rule_of_thumb_n_per_arm,
    size_holdout,
)

#: The pre-registered worked example.
BASELINE = 3_500
MDE = 1_000
PLANNED_N = 384


def float_n_per_arm(baseline: float, mde: float, alpha: float, power: float) -> float:
    """The textbook formula in floating point, as an independent reference."""
    reference = NormalDist()
    z_alpha = reference.inv_cdf(1 - alpha / 2)
    z_beta = reference.inv_cdf(power)
    treated = baseline + mde
    variance = baseline * (1 - baseline) + treated * (1 - treated)
    return (z_alpha + z_beta) ** 2 * variance / mde**2


class TestTheWorkedExample:
    def test_the_exact_formula_gives_three_seven_three(self) -> None:
        """(1.959964 + 0.841621)^2 * 0.475 / 0.01 = 372.82, rounded up."""
        assert required_n_per_arm(BASELINE, MDE) == 373

    def test_the_rule_of_thumb_gives_three_eight_four(self) -> None:
        """16 * 0.40 * 0.60 / 0.01 = 384. The value section 7 quotes."""
        assert rule_of_thumb_n_per_arm(BASELINE, MDE) == 384

    def test_the_rule_of_thumb_is_the_more_conservative_of_the_two(self) -> None:
        """It rounds the constant 15.698 up to 16 and uses a pooled variance of
        0.48 where the actual variances sum to 0.475. Both roundings go the
        same way, so it always asks for at least as much."""
        for baseline in range(500, 9_000, 500):
            for mde in (250, 500, 1_000):
                if baseline + mde > 10_000:
                    continue
                assert rule_of_thumb_n_per_arm(baseline, mde) >= required_n_per_arm(baseline, mde)

    def test_the_planned_figure_exceeds_the_requirement(self) -> None:
        """Which is why storing 384 as the plan is sound: over-powered is not
        an error, and nothing downstream breaks."""
        assert PLANNED_N > required_n_per_arm(BASELINE, MDE)

    def test_the_gap_between_them_is_eleven_cases(self) -> None:
        assert rule_of_thumb_n_per_arm(BASELINE, MDE) - required_n_per_arm(BASELINE, MDE) == 11


class TestRequiredSampleSize:
    def test_it_matches_the_float_reference(self) -> None:
        for baseline_bps in range(500, 9_500, 250):
            for mde_bps in (200, 500, 1_000, 2_000):
                if baseline_bps + mde_bps > 10_000:
                    continue
                expected = float_n_per_arm(baseline_bps / 10_000, mde_bps / 10_000, 0.05, 0.80)
                actual = required_n_per_arm(baseline_bps, mde_bps)
                assert actual == math.ceil(expected - 1e-9), (baseline_bps, mde_bps)

    def test_it_always_rounds_up(self) -> None:
        """A fractional case cannot be enrolled, and rounding down would claim
        power the design does not have."""
        for mde_bps in range(200, 3_000, 137):
            exact = float_n_per_arm(0.35, mde_bps / 10_000, 0.05, 0.80)
            assert required_n_per_arm(BASELINE, mde_bps) >= exact

    def test_a_smaller_effect_needs_more_cases(self) -> None:
        previous = 0
        for mde_bps in (3_000, 2_000, 1_000, 500, 250, 100):
            current = required_n_per_arm(BASELINE, mde_bps)
            assert current > previous, mde_bps
            previous = current

    def test_halving_the_effect_roughly_quadruples_the_sample(self) -> None:
        big = required_n_per_arm(BASELINE, 1_000)
        small = required_n_per_arm(BASELINE, 500)
        assert 3.5 < small / big < 4.5

    def test_more_power_needs_more_cases(self) -> None:
        assert required_n_per_arm(BASELINE, MDE, power_bps=9_000) > required_n_per_arm(
            BASELINE, MDE, power_bps=8_000
        )

    def test_a_tighter_alpha_needs_more_cases(self) -> None:
        assert required_n_per_arm(BASELINE, MDE, alpha_bps=100) > required_n_per_arm(
            BASELINE, MDE, alpha_bps=500
        )

    def test_the_defaults_are_the_pre_registered_parameters(self) -> None:
        assert DEFAULT_ALPHA_BPS == 500
        assert DEFAULT_POWER_BPS == 8_000
        assert required_n_per_arm(BASELINE, MDE) == required_n_per_arm(
            BASELINE, MDE, alpha_bps=500, power_bps=8_000
        )

    def test_a_baseline_near_one_half_needs_the_most(self) -> None:
        """Bernoulli variance peaks at one half, so that is the hardest place
        to detect a given effect."""
        middle = required_n_per_arm(4_500, 500)
        assert middle > required_n_per_arm(500, 500)
        assert middle > required_n_per_arm(9_000, 500)


class TestSampleSizeValidation:
    def test_an_effect_past_full_scale_is_rejected(self) -> None:
        with pytest.raises(PowerError, match="cannot pass 100%"):
            required_n_per_arm(9_500, 1_000)

    def test_a_zero_effect_is_rejected(self) -> None:
        with pytest.raises(PowerError, match="at least 1"):
            required_n_per_arm(BASELINE, 0)

    def test_a_negative_effect_is_rejected(self) -> None:
        with pytest.raises(PowerError, match="at least 1"):
            required_n_per_arm(BASELINE, -100)

    def test_a_baseline_outside_the_scale_is_rejected(self) -> None:
        with pytest.raises(PowerError, match="baseline_bps"):
            required_n_per_arm(-1, MDE)
        with pytest.raises(PowerError, match="baseline_bps"):
            required_n_per_arm(10_001, MDE)

    def test_a_boolean_is_not_a_basis_point_count(self) -> None:
        with pytest.raises(PowerError, match="must be an integer"):
            required_n_per_arm(True, MDE)  # type: ignore[arg-type]


class TestUnequalAllocation:
    def test_an_even_split_reduces_to_the_balanced_formula(self) -> None:
        assert required_n_holdout(
            BASELINE, MDE, holdout_bps=BALANCED_HOLDOUT_BPS
        ) == required_n_per_arm(BASELINE, MDE)

    def test_a_smaller_holdout_arm_shrinks_toward_a_floor(self) -> None:
        """Counter-intuitive but correct: as the treated arm grows it stops
        contributing variance, so the holdout requirement falls toward
        `(z_a + z_b)^2 * p_c(1-p_c) / delta^2` — about 179 here — rather than
        rising. The cost of an uneven split lands on the total, not the arm."""
        floor = 179
        assert required_n_holdout(BASELINE, MDE, holdout_bps=5_000) == 373
        assert required_n_holdout(BASELINE, MDE, holdout_bps=1_000) == 201
        assert required_n_holdout(BASELINE, MDE, holdout_bps=100) == 181
        assert required_n_holdout(BASELINE, MDE, holdout_bps=1) >= floor

    def test_a_smaller_holdout_needs_a_far_larger_total(self) -> None:
        """This is where the cost actually shows up: an even split needs 746
        cases, a 1% holdout needs about 18,100, because the small arm still has
        to reach its floor and drags 99 treated cases along per unit."""

        def total(holdout_bps: int) -> int:
            arm = required_n_holdout(BASELINE, MDE, holdout_bps=holdout_bps)
            return arm + -(-arm * (10_000 - holdout_bps) // holdout_bps)

        assert total(5_000) == 746
        assert total(100) > 18_000
        assert total(100) > 20 * total(5_000)

    def test_the_arm_is_monotone_in_the_holdout_share(self) -> None:
        previous = 0
        for holdout_bps in (500, 1_000, 2_000, 3_000, 4_000, 5_000):
            current = required_n_holdout(BASELINE, MDE, holdout_bps=holdout_bps)
            assert current >= previous, holdout_bps
            previous = current

    def test_an_empty_arm_is_rejected(self) -> None:
        for bad in (0, 10_000, -1, 20_000):
            with pytest.raises(PowerError, match="holdout_bps"):
                required_n_holdout(BASELINE, MDE, holdout_bps=bad)


class TestDetectableEffect:
    def test_it_inverts_the_sample_size(self) -> None:
        for mde_bps in (250, 500, 1_000, 2_000):
            needed = required_n_per_arm(BASELINE, mde_bps)
            assert mde_for_n(needed, BASELINE) <= mde_bps

    def test_the_answer_is_the_smallest_effect_that_fits(self) -> None:
        """One basis point tighter must not fit, or it was not the smallest."""
        for n in (100, 373, 1_000, 5_000):
            found = mde_for_n(n, BASELINE)
            assert required_n_per_arm(BASELINE, found) <= n
            if found > 1:
                assert required_n_per_arm(BASELINE, found - 1) > n

    def test_the_pre_registered_sample_detects_ten_points(self) -> None:
        assert mde_for_n(373, BASELINE) == MDE

    def test_more_cases_detect_smaller_effects(self) -> None:
        previous = 10_001
        for n in (100, 400, 1_000, 4_000, 10_000):
            current = mde_for_n(n, BASELINE)
            assert current < previous, n
            previous = current

    def test_an_underpowered_subgroup_gets_an_honest_number(self) -> None:
        """A null result from 40 cases means almost nothing on its own. That
        those cases could only have detected an effect several times larger
        than the pre-registered one is a fact a reader can weigh."""
        detectable = mde_for_n(40, BASELINE)
        assert detectable > MDE

    def test_too_few_cases_to_detect_anything_is_refused(self) -> None:
        with pytest.raises(PowerError, match="cannot detect any effect"):
            mde_for_n(1, BASELINE)

    def test_a_nonsense_sample_is_rejected(self) -> None:
        with pytest.raises(PowerError, match="n_per_arm"):
            mde_for_n(0, BASELINE)


class TestHoldoutSizing:
    def test_the_pre_registered_design_at_a_realistic_volume(self) -> None:
        plan = size_holdout(20_000, BASELINE, MDE)
        assert plan.required_n_holdout == 373
        assert plan.weekly_volume == 20_000 * 12 // 52
        assert plan.weeks_to_significance >= 1

    def test_a_week_is_twelve_fifty_seconds_of_a_month(self) -> None:
        """Not a four-week approximation, which would drift by a month a year."""
        assert size_holdout(5_200, BASELINE, MDE).weekly_volume == 1_200

    def test_more_volume_finishes_sooner(self) -> None:
        previous = 10**9
        for volume in (2_000, 10_000, 50_000, 200_000):
            weeks = size_holdout(volume, BASELINE, MDE).weeks_to_significance
            assert weeks <= previous, volume
            previous = weeks

    def test_a_smaller_holdout_takes_longer(self) -> None:
        """The trade-off the calculator exists to make visible: holding back
        less costs less revenue but takes longer to learn anything. At 2,000
        cases a month an even split answers in two weeks and a 10% holdout
        takes five."""
        balanced = size_holdout(2_000, BASELINE, MDE, holdout_bps=5_000)
        tenth = size_holdout(2_000, BASELINE, MDE, holdout_bps=1_000)
        assert balanced.weeks_to_significance == 2
        assert tenth.weeks_to_significance == 5

    def test_a_smaller_holdout_forgoes_less_revenue(self) -> None:
        balanced = size_holdout(2_000, BASELINE, MDE, holdout_bps=5_000, avg_amount_minor=200_000)
        tenth = size_holdout(2_000, BASELINE, MDE, holdout_bps=1_000, avg_amount_minor=200_000)
        assert tenth.revenue_forgone is not None and balanced.revenue_forgone is not None
        assert tenth.revenue_forgone < balanced.revenue_forgone

    def test_revenue_forgone_is_the_lift_given_up_on_held_out_cases(self) -> None:
        plan = size_holdout(20_000, BASELINE, MDE, avg_amount_minor=100_000)
        expected = plan.holdout_cases_at_completion * MDE * 100_000 // 10_000
        assert plan.revenue_forgone == expected

    def test_an_unsupplied_case_value_reports_unknown_not_zero(self) -> None:
        """An unknown cost stated as zero is a claim the module cannot make."""
        plan = size_holdout(20_000, BASELINE, MDE)
        assert plan.revenue_forgone is None
        assert plan.avg_amount_minor is None

    def test_both_arms_are_sized(self) -> None:
        plan = size_holdout(20_000, BASELINE, MDE, holdout_bps=2_000)
        assert plan.required_n_treatment == plan.required_n_holdout * 8_000 // 2_000

    def test_the_completion_totals_are_consistent(self) -> None:
        plan = size_holdout(20_000, BASELINE, MDE, holdout_bps=2_000)
        assert plan.holdout_cases_at_completion >= plan.required_n_holdout
        assert plan.total_cases_at_completion >= plan.holdout_cases_at_completion

    def test_a_volume_too_small_to_ever_finish_is_refused(self) -> None:
        with pytest.raises(PowerError, match="never finish"):
            size_holdout(10, BASELINE, MDE, holdout_bps=100)

    def test_a_nonsense_volume_is_rejected(self) -> None:
        with pytest.raises(PowerError, match="monthly_volume"):
            size_holdout(0, BASELINE, MDE)

    def test_a_negative_case_value_is_rejected(self) -> None:
        with pytest.raises(PowerError, match="avg_amount_minor"):
            size_holdout(20_000, BASELINE, MDE, avg_amount_minor=-1)

    def test_the_plan_serialises_without_a_float(self) -> None:
        plan = size_holdout(20_000, BASELINE, MDE, avg_amount_minor=200_000)
        for value in plan.as_dict().values():
            assert not isinstance(value, float), value

    def test_the_plan_carries_its_parameters(self) -> None:
        plan = size_holdout(20_000, BASELINE, MDE, avg_amount_minor=200_000)
        payload = plan.as_dict()
        assert payload["alpha_bps"] == DEFAULT_ALPHA_BPS
        assert payload["power_bps"] == DEFAULT_POWER_BPS
        assert payload["baseline_bps"] == BASELINE
        assert payload["mde_bps"] == MDE

    def test_the_plan_is_frozen(self) -> None:
        plan = size_holdout(20_000, BASELINE, MDE)
        with pytest.raises(AttributeError):
            plan.weeks_to_significance = 1  # type: ignore[misc]


class TestUnderpoweredLabel:
    def test_below_the_plan_is_underpowered(self) -> None:
        assert is_underpowered(300, PLANNED_N)

    def test_at_the_plan_is_not(self) -> None:
        assert not is_underpowered(PLANNED_N, PLANNED_N)

    def test_above_the_plan_is_not(self) -> None:
        assert not is_underpowered(10_000, PLANNED_N)

    def test_it_compares_against_the_plan_not_a_recomputed_requirement(self) -> None:
        """Section 9: the comparison is to the pre-registered figure. Deriving
        a fresh requirement from the observed data would be the post-hoc move a
        fixed horizon exists to prevent — with 373 achieved you would 'meet'
        a requirement of 373 and never show the label."""
        achieved = 373
        assert is_underpowered(achieved, PLANNED_N)
        assert not is_underpowered(achieved, required_n_per_arm(BASELINE, MDE))


class TestPurity:
    """Power is arithmetic. It reads nothing and decides nothing."""

    @staticmethod
    def _tree() -> ast.Module:
        from app.causal import power as module

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
        for banned in ("float", "Decimal", "sqrt", "exp", "log"):
            assert banned not in self._identifiers(), banned

    def test_it_depends_only_on_the_fixed_point_normal(self) -> None:
        assert self._imports() == {"__future__", "dataclasses", "app.causal.normal"}

    def test_it_reads_no_clock_and_draws_no_randomness(self) -> None:
        for banned in ("now", "utcnow", "today", "random", "choices"):
            assert banned not in self._identifiers(), banned

    def test_it_touches_no_database(self) -> None:
        for banned in ("Session", "select", "execute", "commit", "flush", "session"):
            assert banned not in self._identifiers(), banned

    def test_it_never_names_ground_truth(self) -> None:
        for name in self._identifiers():
            assert not name.startswith("truth_"), name

    def test_it_imports_no_generator_module(self) -> None:
        for module in self._imports():
            assert not module.startswith("simulator"), module

    def test_it_invents_no_economic_input(self) -> None:
        """`avg_amount_minor` is a caller parameter with no default value, not
        an assumed constant. Gross margin and lifetime value appear nowhere."""
        for banned in ("gross_margin", "avg_customer_ltv", "harm_cost"):
            assert banned not in self._identifiers(), banned
