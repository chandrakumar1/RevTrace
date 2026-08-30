"""Covariate balance — the arithmetic, and the guarantees around it.

Balance is entirely a computation over already-loaded rows, so almost all of it
is testable without a database. The loading half — the join, the ITT
denominator, the payment-method lookup — is in
`tests/integration/test_day3_balance.py`.

Floats appear freely *here*, as an independent reference to check the integer
implementation against. They must never appear in the implementation.
"""

from __future__ import annotations

import ast
import inspect
import math
import pathlib
import random
import uuid

import pytest

from app.causal.balance import (
    BALANCE_COVARIATES,
    CATEGORICAL,
    CONTINUOUS,
    IMBALANCE_THRESHOLD_BPS,
    MISSING_LEVEL,
    BalanceError,
    CovariateRow,
    Smd,
    balance_report,
    categorical_balance,
    continuous_balance,
    mean_smd_bps,
    proportion_smd_bps,
    round_sqrt_ratio,
    split_stratum_key,
)
from app.models.enums import Arm, PaymentMethod, RiskType

TREATMENT = Arm.TREATMENT.value
HOLDOUT = Arm.HOLDOUT.value


def row(
    arm: str,
    *,
    risk_type: str = RiskType.REPEATED_PAYMENT_FAILURE.value,
    amount_band: str = "2000-5000",
    amount_at_risk: int = 230_400,
    confidence_bps: int = 7_000,
    payment_method: str | None = PaymentMethod.CARD.value,
) -> CovariateRow:
    return CovariateRow(
        risk_id=uuid.uuid4(),
        arm=arm,
        risk_type=risk_type,
        amount_band=amount_band,
        amount_at_risk=amount_at_risk,
        confidence_bps=confidence_bps,
        payment_method=payment_method,
    )


def float_smd(treatment: list[int], holdout: list[int]) -> float:
    """The textbook formula in floating point, as an independent reference."""

    def variance(values: list[int]) -> float:
        mean = sum(values) / len(values)
        return sum((v - mean) ** 2 for v in values) / (len(values) - 1)

    diff = sum(treatment) / len(treatment) - sum(holdout) / len(holdout)
    pooled = (variance(treatment) + variance(holdout)) / 2
    return diff / math.sqrt(pooled)


class TestIntegerSquareRoot:
    def test_exact_roots(self) -> None:
        assert round_sqrt_ratio(4, 1) == 2
        assert round_sqrt_ratio(25, 1) == 5
        assert round_sqrt_ratio(0, 1) == 0

    def test_it_rounds_to_nearest(self) -> None:
        assert round_sqrt_ratio(2, 1) == 1  # 1.414
        assert round_sqrt_ratio(3, 1) == 2  # 1.732
        assert round_sqrt_ratio(24, 100) == 0  # 0.489

    def test_a_half_rounds_up(self) -> None:
        assert round_sqrt_ratio(9, 4) == 2  # exactly 1.5
        assert round_sqrt_ratio(1, 4) == 1  # exactly 0.5
        assert round_sqrt_ratio(25, 4) == 3  # exactly 2.5

    def test_it_matches_the_float_root_over_a_wide_range(self) -> None:
        rng = random.Random(42)
        for _ in range(2_000):
            num = rng.randrange(0, 10**14)
            den = rng.randrange(1, 10**7)
            assert round_sqrt_ratio(num, den) == round(math.sqrt(num / den) + 1e-12)

    def test_it_rejects_a_nonsense_ratio(self) -> None:
        with pytest.raises(BalanceError, match="denominator must be positive"):
            round_sqrt_ratio(1, 0)
        with pytest.raises(BalanceError, match="numerator must be non-negative"):
            round_sqrt_ratio(-1, 1)


class TestContinuousSmd:
    def test_a_hand_computable_case(self) -> None:
        """[1,2,3] vs [4,5,6]: means differ by 3, each variance is 1."""
        result = mean_smd_bps([1, 2, 3], [4, 5, 6])
        assert result.smd_bps == -30_000

    def test_identical_arms_are_perfectly_balanced(self) -> None:
        values = [10, 20, 30, 40]
        assert mean_smd_bps(values, list(values)).smd_bps == 0

    def test_the_sign_is_treatment_minus_holdout(self) -> None:
        assert mean_smd_bps([10, 20, 30], [1, 2, 3]).smd_bps > 0
        assert mean_smd_bps([1, 2, 3], [10, 20, 30]).smd_bps < 0

    def test_swapping_the_arms_negates_it_exactly(self) -> None:
        forward = mean_smd_bps([3, 9, 14, 2], [5, 5, 21, 8])
        reverse = mean_smd_bps([5, 5, 21, 8], [3, 9, 14, 2])
        assert forward.smd_bps == -(reverse.smd_bps or 0)

    def test_it_matches_the_float_reference(self) -> None:
        rng = random.Random(7)
        for _ in range(500):
            treatment = [rng.randrange(0, 5_000_000) for _ in range(rng.randrange(2, 40))]
            holdout = [rng.randrange(0, 5_000_000) for _ in range(rng.randrange(2, 40))]
            if len(set(treatment)) == 1 and len(set(holdout)) == 1:
                continue
            result = mean_smd_bps(treatment, holdout)
            assert result.smd_bps is not None
            assert abs(result.smd_bps - float_smd(treatment, holdout) * 10_000) <= 1

    def test_it_is_scale_free(self) -> None:
        """Multiplying every value by a constant leaves the SMD unchanged —
        which is the whole point of standardising."""
        base = mean_smd_bps([3, 5, 11], [2, 9, 4])
        scaled = mean_smd_bps([300, 500, 1100], [200, 900, 400])
        assert base.smd_bps == scaled.smd_bps

    def test_paise_sized_values_stay_exact(self) -> None:
        treatment = [1_250_000, 980_000, 1_500_000, 2_400_000]
        holdout = [1_240_000, 990_000, 1_510_000, 2_390_000]
        result = mean_smd_bps(treatment, holdout)
        assert result.smd_bps is not None
        assert abs(result.smd_bps - float_smd(treatment, holdout) * 10_000) <= 1

    def test_a_single_unit_arm_has_no_variance(self) -> None:
        result = mean_smd_bps([5], [1, 2, 3])
        assert result.smd_bps is None
        assert "at least two units" in (result.undefined_reason or "")

    def test_an_empty_arm_is_undefined(self) -> None:
        assert mean_smd_bps([], []).smd_bps is None

    def test_constant_arms_with_equal_means_are_balanced(self) -> None:
        result = mean_smd_bps([7, 7, 7], [7, 7, 7])
        assert result.smd_bps == 0

    def test_constant_arms_with_different_means_cannot_be_standardised(self) -> None:
        result = mean_smd_bps([7, 7, 7], [9, 9, 9])
        assert result.smd_bps is None
        assert "pooled variance is zero" in (result.undefined_reason or "")
        assert result.flagged

    def test_a_float_value_is_rejected_at_the_boundary(self) -> None:
        with pytest.raises(BalanceError, match="must be integers"):
            mean_smd_bps([1.5, 2.0, 3.0], [1, 2, 3])  # type: ignore[list-item]

    def test_a_bool_is_not_an_integer_here(self) -> None:
        with pytest.raises(BalanceError, match="must be integers"):
            mean_smd_bps([True, False, True], [1, 0, 1])  # type: ignore[list-item]

    def test_the_arm_sizes_are_carried_through(self) -> None:
        result = mean_smd_bps([1, 2, 3], [4, 5])
        assert (result.treatment_n, result.holdout_n) == (3, 2)


class TestProportionSmd:
    def test_a_hand_computable_case(self) -> None:
        """50/100 vs 40/100: diff 0.1, pooled sd sqrt(0.245) = 0.49497."""
        assert proportion_smd_bps(50, 100, 40, 100).smd_bps == 2_020

    def test_equal_proportions_are_balanced(self) -> None:
        assert proportion_smd_bps(30, 100, 60, 200).smd_bps == 0

    def test_the_sign_is_treatment_minus_holdout(self) -> None:
        assert proportion_smd_bps(60, 100, 40, 100).smd_bps > 0
        assert proportion_smd_bps(40, 100, 60, 100).smd_bps < 0

    def test_swapping_the_arms_negates_it_exactly(self) -> None:
        forward = proportion_smd_bps(37, 111, 52, 143)
        reverse = proportion_smd_bps(52, 143, 37, 111)
        assert forward.smd_bps == -(reverse.smd_bps or 0)

    def test_it_matches_the_float_reference(self) -> None:
        rng = random.Random(11)
        for _ in range(500):
            n_t, n_h = rng.randrange(2, 500), rng.randrange(2, 500)
            x_t, x_h = rng.randrange(0, n_t + 1), rng.randrange(0, n_h + 1)
            p_t, p_h = x_t / n_t, x_h / n_h
            pooled = (p_t * (1 - p_t) + p_h * (1 - p_h)) / 2
            if pooled == 0:
                continue
            expected = (p_t - p_h) / math.sqrt(pooled) * 10_000
            result = proportion_smd_bps(x_t, n_t, x_h, n_h)
            assert result.smd_bps is not None
            assert abs(result.smd_bps - expected) <= 1

    def test_a_level_absent_from_both_arms_is_balanced(self) -> None:
        assert proportion_smd_bps(0, 100, 0, 100).smd_bps == 0

    def test_a_level_present_in_only_one_arm_cannot_be_standardised(self) -> None:
        """Everyone treated has it, nobody in holdout does: zero variance in
        both arms, means far apart. Undefined, and certainly not balanced."""
        result = proportion_smd_bps(100, 100, 0, 100)
        assert result.smd_bps is None
        assert result.flagged

    def test_an_empty_arm_is_undefined(self) -> None:
        assert proportion_smd_bps(0, 0, 5, 10).smd_bps is None

    def test_impossible_counts_are_rejected(self) -> None:
        with pytest.raises(BalanceError, match="treatment_hits"):
            proportion_smd_bps(101, 100, 5, 10)
        with pytest.raises(BalanceError, match="holdout_hits"):
            proportion_smd_bps(5, 100, -1, 10)


class TestThreshold:
    def test_the_threshold_is_the_basis_point_form_of_one_tenth(self) -> None:
        assert IMBALANCE_THRESHOLD_BPS == 1_000

    def test_exactly_at_the_threshold_is_not_flagged(self) -> None:
        assert not Smd(smd_bps=1_000, treatment_n=10, holdout_n=10).flagged
        assert not Smd(smd_bps=-1_000, treatment_n=10, holdout_n=10).flagged

    def test_one_basis_point_over_is_flagged(self) -> None:
        assert Smd(smd_bps=1_001, treatment_n=10, holdout_n=10).flagged
        assert Smd(smd_bps=-1_001, treatment_n=10, holdout_n=10).flagged

    def test_the_flag_is_symmetric_in_sign(self) -> None:
        for magnitude in (0, 999, 1_000, 1_001, 30_000):
            positive = Smd(smd_bps=magnitude, treatment_n=9, holdout_n=9)
            negative = Smd(smd_bps=-magnitude, treatment_n=9, holdout_n=9)
            assert positive.flagged == negative.flagged

    def test_undefined_is_flagged(self) -> None:
        """An unverifiable covariate must not read the same as a checked one."""
        undefined = Smd(smd_bps=None, treatment_n=1, holdout_n=1, undefined_reason="too few")
        assert undefined.flagged
        assert not undefined.is_defined


class TestStratumKey:
    def test_it_splits_into_risk_type_and_band(self) -> None:
        assert split_stratum_key("repeated_payment_failure|2000-5000") == (
            "repeated_payment_failure",
            "2000-5000",
        )

    def test_it_round_trips_what_assignment_writes(self) -> None:
        from app.experiments.assignment import AMOUNT_BANDS, stratum_key

        for risk_type in RiskType.values():
            for _, upper in AMOUNT_BANDS:
                key = stratum_key(risk_type, upper - 1)
                assert split_stratum_key(key)[0] == risk_type

    def test_the_top_band_survives_its_greater_than_sign(self) -> None:
        from app.experiments.assignment import TOP_AMOUNT_BAND, stratum_key

        key = stratum_key(RiskType.CHECKOUT_ABANDONMENT.value, 9_999_999)
        assert split_stratum_key(key)[1] == TOP_AMOUNT_BAND

    def test_a_malformed_key_is_rejected(self) -> None:
        for bad in ("no-separator", "|missing-type", "missing-band|"):
            with pytest.raises(BalanceError, match="malformed stratum_key"):
                split_stratum_key(bad)


class TestCategoricalBalance:
    def test_one_level_per_observed_value(self) -> None:
        rows = [
            row(TREATMENT, payment_method="card"),
            row(TREATMENT, payment_method="upi"),
            row(HOLDOUT, payment_method="card"),
            row(HOLDOUT, payment_method="netbanking"),
        ]
        result = categorical_balance("payment_method", rows, lambda r: r.payment_method)
        assert [level.level for level in result.levels] == ["card", "netbanking", "upi"]
        assert result.kind == CATEGORICAL

    def test_counts_are_per_arm(self) -> None:
        rows = [row(TREATMENT, payment_method="upi")] * 3 + [row(HOLDOUT, payment_method="upi")]
        result = categorical_balance("payment_method", rows, lambda r: r.payment_method)
        assert (result.levels[0].treatment_count, result.levels[0].holdout_count) == (3, 1)

    def test_a_missing_value_becomes_an_explicit_level(self) -> None:
        rows = [
            row(TREATMENT, payment_method=None),
            row(TREATMENT, payment_method="card"),
            row(HOLDOUT, payment_method=None),
            row(HOLDOUT, payment_method="card"),
        ]
        result = categorical_balance("payment_method", rows, lambda r: r.payment_method)
        assert MISSING_LEVEL in [level.level for level in result.levels]

    def test_missing_sorts_last(self) -> None:
        rows = [
            row(TREATMENT, payment_method=None),
            row(TREATMENT, payment_method="wallet"),
            row(HOLDOUT, payment_method=None),
            row(HOLDOUT, payment_method="card"),
        ]
        result = categorical_balance("payment_method", rows, lambda r: r.payment_method)
        assert result.levels[-1].level == MISSING_LEVEL

    def test_the_denominator_is_the_arm_not_the_covered_subset(self) -> None:
        """Balance on a covariate that is absent for part of the population is
        computed against the randomised arm size. Using only the units that
        have a value would measure balance on a selected subpopulation."""
        rows = [row(TREATMENT, payment_method=None) for _ in range(6)]
        rows += [row(TREATMENT, payment_method="card") for _ in range(4)]
        rows += [row(HOLDOUT, payment_method=None) for _ in range(2)]
        rows += [row(HOLDOUT, payment_method="card") for _ in range(8)]

        result = categorical_balance("payment_method", rows, lambda r: r.payment_method)
        for level in result.levels:
            assert level.smd.treatment_n == 10
            assert level.smd.holdout_n == 10

        counts = {
            level.level: level.treatment_count + level.holdout_count for level in result.levels
        }
        assert sum(counts.values()) == 20

    def test_lopsided_coverage_is_flagged(self) -> None:
        """60% missing in one arm and 20% in the other is an imbalance in its
        own right, and the report must say so rather than drop the rows."""
        rows = [row(TREATMENT, payment_method=None) for _ in range(6)]
        rows += [row(TREATMENT, payment_method="card") for _ in range(4)]
        rows += [row(HOLDOUT, payment_method=None) for _ in range(2)]
        rows += [row(HOLDOUT, payment_method="card") for _ in range(8)]

        result = categorical_balance("payment_method", rows, lambda r: r.payment_method)
        assert result.flagged
        assert MISSING_LEVEL in result.flagged_levels

    def test_an_evenly_split_category_is_balanced(self) -> None:
        rows = [row(TREATMENT, payment_method="card"), row(TREATMENT, payment_method="upi")] * 25
        rows += [row(HOLDOUT, payment_method="card"), row(HOLDOUT, payment_method="upi")] * 25
        result = categorical_balance("payment_method", rows, lambda r: r.payment_method)
        assert not result.flagged
        assert all(level.smd.smd_bps == 0 for level in result.levels)

    def test_levels_are_ordered_deterministically(self) -> None:
        rows = [row(TREATMENT, payment_method=m) for m in ("upi", "card", "wallet", "emi")]
        rows += [row(HOLDOUT, payment_method=m) for m in ("wallet", "emi", "upi", "card")]
        first = categorical_balance("payment_method", rows, lambda r: r.payment_method)
        second = categorical_balance(
            "payment_method", list(reversed(rows)), lambda r: r.payment_method
        )
        assert [x.level for x in first.levels] == [x.level for x in second.levels]


class TestContinuousCovariate:
    def test_it_reports_a_single_smd(self) -> None:
        rows = [row(TREATMENT, amount_at_risk=v) for v in (100, 200, 300)]
        rows += [row(HOLDOUT, amount_at_risk=v) for v in (100, 200, 300)]
        result = continuous_balance("amount_at_risk", rows, lambda r: r.amount_at_risk)
        assert result.kind == CONTINUOUS
        assert result.levels == ()
        assert result.smd is not None and result.smd.smd_bps == 0

    def test_a_richer_treatment_arm_shows_positive(self) -> None:
        rows = [row(TREATMENT, amount_at_risk=v) for v in (900_000, 950_000, 1_100_000)]
        rows += [row(HOLDOUT, amount_at_risk=v) for v in (100_000, 150_000, 120_000)]
        result = continuous_balance("amount_at_risk", rows, lambda r: r.amount_at_risk)
        assert result.smd is not None and result.smd.smd_bps is not None
        assert result.smd.smd_bps > IMBALANCE_THRESHOLD_BPS
        assert result.flagged


class TestBalanceReport:
    @staticmethod
    def a_balanced_population(size: int = 200) -> list[CovariateRow]:
        """Matched pairs, so the two arms hold identical distributions.

        Deliberately not a random split: with 100 units per arm the standard
        error of an SMD is about 0.14, so a fair draw exceeds the 0.1 threshold
        roughly half the time. A fixture that is *exactly* balanced is the only
        one that can assert an exact verdict.
        """
        rng = random.Random(2026)
        rows: list[CovariateRow] = []
        for _ in range(size // 2):
            amount = 300_000 + rng.randrange(0, 100) * 100
            for arm in (TREATMENT, HOLDOUT):
                rows.append(
                    row(
                        arm,
                        risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value,
                        amount_band="2000-5000",
                        amount_at_risk=amount,
                        confidence_bps=7_000,
                        payment_method="card",
                    )
                )
        return rows

    def test_it_covers_the_pre_registered_covariates_in_order(self) -> None:
        report = balance_report(self.a_balanced_population())
        assert tuple(c.name for c in report.covariates) == BALANCE_COVARIATES

    def test_the_covariates_match_the_pre_registration(self) -> None:
        assert BALANCE_COVARIATES == (
            "risk_type",
            "amount_band",
            "amount_at_risk",
            "confidence_bps",
            "payment_method",
        )

    def test_arm_counts_are_the_randomised_population(self) -> None:
        report = balance_report(self.a_balanced_population(200))
        assert (report.treatment_n, report.holdout_n, report.total_n) == (100, 100, 200)

    def test_a_balanced_population_is_reported_balanced(self) -> None:
        report = balance_report(self.a_balanced_population())
        assert report.is_balanced
        assert report.flagged == ()

    def test_a_stacked_arm_is_flagged_by_name(self) -> None:
        rows = [row(TREATMENT, amount_at_risk=2_000_000) for _ in range(50)]
        rows += [row(TREATMENT, amount_at_risk=1_900_000) for _ in range(50)]
        rows += [row(HOLDOUT, amount_at_risk=100_000) for _ in range(50)]
        rows += [row(HOLDOUT, amount_at_risk=110_000) for _ in range(50)]
        report = balance_report(rows)
        assert "amount_at_risk" in report.flagged
        assert not report.is_balanced

    def test_a_risk_type_skew_is_flagged(self) -> None:
        rows = [row(TREATMENT, risk_type=RiskType.CHECKOUT_ABANDONMENT.value) for _ in range(80)]
        rows += [
            row(TREATMENT, risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value) for _ in range(20)
        ]
        rows += [row(HOLDOUT, risk_type=RiskType.CHECKOUT_ABANDONMENT.value) for _ in range(20)]
        rows += [row(HOLDOUT, risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value) for _ in range(80)]
        report = balance_report(rows)
        assert "risk_type" in report.flagged

    def test_it_is_deterministic(self) -> None:
        rows = self.a_balanced_population()
        assert balance_report(rows).as_dict() == balance_report(list(rows)).as_dict()

    def test_row_order_does_not_change_the_verdict(self) -> None:
        rows = self.a_balanced_population()
        shuffled = list(rows)
        random.Random(5).shuffle(shuffled)
        assert balance_report(rows).as_dict() == balance_report(shuffled).as_dict()

    def test_the_experiment_id_is_carried_through(self) -> None:
        experiment_id = uuid.uuid4()
        report = balance_report(self.a_balanced_population(), experiment_id)
        assert report.experiment_id == experiment_id
        assert report.as_dict()["experiment_id"] == str(experiment_id)

    def test_an_empty_population_is_reported_not_crashed(self) -> None:
        """A report on nothing is undefined, not balanced. Every covariate is
        flagged, including the categorical ones that have no levels at all —
        "nothing was checked" must not render as "nothing was wrong"."""
        report = balance_report([])
        assert (report.treatment_n, report.holdout_n) == (0, 0)
        assert not report.is_balanced
        assert set(report.flagged) == set(BALANCE_COVARIATES)

    def test_a_one_armed_population_is_flagged_not_balanced(self) -> None:
        """Everything in treatment and nothing in holdout is not an experiment,
        and no covariate may report balance against an empty arm."""
        report = balance_report([row(TREATMENT) for _ in range(20)])
        assert (report.treatment_n, report.holdout_n) == (20, 0)
        assert set(report.flagged) == set(BALANCE_COVARIATES)

    def test_the_serialised_report_carries_no_float(self) -> None:
        report = balance_report(self.a_balanced_population())

        def walk(value: object) -> None:
            assert not isinstance(value, float), value
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(report.as_dict())

    def test_the_serialised_report_states_the_threshold(self) -> None:
        assert balance_report([]).as_dict()["threshold_bps"] == IMBALANCE_THRESHOLD_BPS

    def test_worst_smd_picks_the_largest_magnitude(self) -> None:
        rows = [row(TREATMENT, payment_method="card") for _ in range(70)]
        rows += [row(TREATMENT, payment_method="upi") for _ in range(30)]
        rows += [row(HOLDOUT, payment_method="card") for _ in range(50)]
        rows += [row(HOLDOUT, payment_method="upi") for _ in range(50)]
        report = balance_report(rows)
        method = next(c for c in report.covariates if c.name == "payment_method")
        assert method.worst_smd_bps is not None
        assert abs(method.worst_smd_bps) == max(
            abs(level.smd.smd_bps or 0) for level in method.levels
        )


class TestPurity:
    """Balance measures. It does not act, and it forms no float."""

    @staticmethod
    def _tree() -> ast.Module:
        from app.causal import balance as module

        source = pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")
        return ast.parse(source)

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

    def test_there_is_no_true_division_anywhere(self) -> None:
        """`/` is where a float would enter. Every ratio here is carried as a
        numerator and a denominator until a single integer square root."""
        for node in ast.walk(self._tree()):
            assert not isinstance(node, ast.Div), ast.dump(node)

    def test_no_float_is_ever_constructed(self) -> None:
        identifiers = self._identifiers()
        assert "float" not in identifiers
        assert "sqrt" not in identifiers  # math.isqrt only
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), node.value

    def test_it_uses_the_integer_square_root(self) -> None:
        assert "isqrt" in self._identifiers()

    def test_no_scientific_stack_dependency(self) -> None:
        for module in self._imports():
            for banned in ("numpy", "scipy", "pandas", "sklearn", "statsmodels"):
                assert banned not in module.lower(), module

    def test_it_never_reads_ground_truth(self) -> None:
        for name in self._identifiers():
            assert not name.startswith("truth_"), name

    def test_it_writes_nothing(self) -> None:
        identifiers = self._identifiers()
        for banned in ("add", "add_all", "commit", "flush", "merge", "delete", "bulk_save_objects"):
            assert banned not in identifiers, banned

    def test_it_creates_no_recovery_or_policy_concept(self) -> None:
        identifiers = self._identifiers()
        for banned in (
            "RecoveryCase",
            "RecoveryAction",
            "AuditEvent",
            "approve",
            "approved",
            "policy_status",
            "execute_action",
            "recommend",
        ):
            assert banned not in identifiers, banned

    def test_it_estimates_no_effect_and_scores_no_uplift(self) -> None:
        """Balance is a diagnostic. Effect estimation, uplift, and abstention
        are separate components and must not appear here early."""
        identifiers = self._identifiers()
        for banned in (
            "UpliftScore",
            "ExperimentResult",
            "uplift",
            "abstain",
            "AbstainReason",
            "bootstrap",
            "p_value_micros",
        ):
            assert banned not in identifiers, banned

    def test_it_reads_no_clock(self) -> None:
        for name in ("now", "utcnow", "today"):
            assert name not in self._identifiers(), name

    def test_it_reads_only_assignments_risks_and_attempts(self) -> None:
        imported: set[str] = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.ImportFrom) and node.module == "app.models":
                imported.update(alias.name for alias in node.names)
        assert imported == {"CaseAssignment", "PaymentAttempt", "RevenueRisk"}
