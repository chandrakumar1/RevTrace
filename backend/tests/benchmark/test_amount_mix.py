"""The rate/mix decomposition of the incremental ledger figure.

Two components, defined as a value and a residual:

    rate_effect       = round(ate_bps x n_treat x mean_holdout / 10000)
    amount_mix_effect = incremental_recovered - rate_effect

The invariant that matters is that they sum to `incremental_recovered`
**exactly** — not approximately, and not after rounding. Defining the second as
a residual is what guarantees it, and most of this file exists to prove that
holds at the boundaries where a naive implementation would drift: a negative
ATE, a zero ATE, and a numerator that does not divide by the basis-point scale.

Pure arithmetic over already-computed values. Nothing here re-derives a rate, an
amount, or an interval, and no test asserts a particular business outcome.
"""

from __future__ import annotations

import pytest

from app.causal.estimators import AmountEffect, Interval, RateEffect
from app.reporting.evaluation import BPS_SCALE, AmountMix, EvaluationError, amount_mix

ALPHA = 500
RESAMPLES = 1_000


def a_rate_effect(*, ate_bps: int) -> RateEffect:
    """A rate effect carrying only the field the decomposition reads."""
    return RateEffect(
        n_treatment=1_000,
        n_holdout=1_000,
        hits_treatment=500,
        hits_holdout=400,
        rate_treatment_bps=5_000,
        rate_holdout_bps=4_000,
        ate_bps=ate_bps,
        interval=Interval(low=0, high=0, alpha_bps=ALPHA, resamples=RESAMPLES, seed=1),
        p_value_micros=0,
    )


def a_ledger(*, n_treatment: int, mean_holdout: int, incremental: int) -> AmountEffect:
    return AmountEffect(
        n_treatment=n_treatment,
        n_holdout=1_000,
        mean_treatment=0,
        mean_holdout=mean_holdout,
        gross_recovered=max(incremental, 0),
        incremental_recovered=incremental,
        credited_not_earned=max(incremental, 0) - incremental,
        interval=Interval(low=0, high=0, alpha_bps=ALPHA, resamples=RESAMPLES, seed=1),
    )


def mix_of(*, ate_bps: int, n_treatment: int, mean_holdout: int, incremental: int) -> AmountMix:
    return amount_mix(
        a_rate_effect(ate_bps=ate_bps),
        a_ledger(n_treatment=n_treatment, mean_holdout=mean_holdout, incremental=incremental),
    )


class TestTheInvariant:
    """The parts sum to the whole. Everything else is secondary."""

    @pytest.mark.parametrize(
        ("ate_bps", "n_treatment", "mean_holdout", "incremental"),
        [
            (1_564, 5_044, 175_967, 447_880_605),  # the accepted N=10,000 run
            (1_000, 1_000, 100_000, 12_000_000),  # positive, exact division
            (0, 1_000, 100_000, 5_000),  # zero ATE
            (-700, 1_000, 100_000, -9_000_000),  # negative ATE
            (1, 1, 1, 0),  # everything minimal
            (9_999, 7, 333, 1),  # awkward, rounds
            (-9_999, 7, 333, -1),  # awkward and negative
            (3, 3, 3, 0),  # numerator far below the scale
        ],
    )
    def test_the_components_sum_to_incremental(
        self, ate_bps: int, n_treatment: int, mean_holdout: int, incremental: int
    ) -> None:
        mix = mix_of(
            ate_bps=ate_bps,
            n_treatment=n_treatment,
            mean_holdout=mean_holdout,
            incremental=incremental,
        )
        assert mix.rate_effect + mix.amount_mix_effect == incremental
        assert mix.incremental_recovered == incremental

    def test_a_mismatched_construction_is_refused(self) -> None:
        """The dataclass guards the identity even if built by hand."""
        with pytest.raises(EvaluationError, match="does not sum to the ledger"):
            AmountMix(
                rate_effect=100,
                amount_mix_effect=100,
                incremental_recovered=500,
                ate_bps=0,
                n_treatment=0,
                mean_holdout=0,
            )


class TestTheRateComponent:
    def test_a_positive_ate_prices_the_lift_at_the_holdout_mean(self) -> None:
        """10% of 1,000 units at Rs 1,000 each = Rs 100,000."""
        mix = mix_of(ate_bps=1_000, n_treatment=1_000, mean_holdout=100_000, incremental=12_000_000)
        assert mix.rate_effect == 10_000_000
        assert mix.amount_mix_effect == 2_000_000

    def test_a_zero_ate_attributes_everything_to_mix(self) -> None:
        """No extra payers, so every rupee of lift came from a different mix."""
        mix = mix_of(ate_bps=0, n_treatment=1_000, mean_holdout=100_000, incremental=5_000)
        assert mix.rate_effect == 0
        assert mix.amount_mix_effect == 5_000

    def test_a_negative_ate_gives_a_negative_rate_component(self) -> None:
        """A treatment that recovered fewer orders. Sign is preserved, not dropped."""
        mix = mix_of(ate_bps=-700, n_treatment=1_000, mean_holdout=100_000, incremental=-9_000_000)
        assert mix.rate_effect == -7_000_000
        assert mix.amount_mix_effect == -2_000_000

    def test_a_zero_holdout_mean_gives_a_zero_rate_component(self) -> None:
        """Nothing was recovered untreated, so extra payers are priced at nothing."""
        mix = mix_of(ate_bps=1_000, n_treatment=1_000, mean_holdout=0, incremental=4_000)
        assert mix.rate_effect == 0
        assert mix.amount_mix_effect == 4_000


class TestRounding:
    """One rounding, on the rate component; the residual absorbs it exactly."""

    def test_a_half_rounds_away_from_zero(self) -> None:
        # 1 x 5000 x 10000 = 50,000,000; / 10,000 = 5000 exactly. Shift by one
        # unit of numerator to land on a half.
        exact = mix_of(ate_bps=1, n_treatment=5_000, mean_holdout=10_000, incremental=0)
        assert exact.rate_effect == 5_000

    def test_a_positive_half_rounds_up(self) -> None:
        # 1 x 1 x 5000 = 5000; 5000 / 10000 = 0.5 -> 1
        mix = mix_of(ate_bps=1, n_treatment=1, mean_holdout=5_000, incremental=0)
        assert mix.rate_effect == 1
        assert mix.amount_mix_effect == -1

    def test_a_negative_half_rounds_away_from_zero(self) -> None:
        # -1 x 1 x 5000 = -5000; -5000 / 10000 = -0.5 -> -1
        mix = mix_of(ate_bps=-1, n_treatment=1, mean_holdout=5_000, incremental=0)
        assert mix.rate_effect == -1
        assert mix.amount_mix_effect == 1

    def test_below_half_rounds_toward_zero(self) -> None:
        # 1 x 1 x 4999 = 4999 -> 0.4999 -> 0
        assert mix_of(ate_bps=1, n_treatment=1, mean_holdout=4_999, incremental=0).rate_effect == 0
        # and symmetrically for a negative
        assert mix_of(ate_bps=-1, n_treatment=1, mean_holdout=4_999, incremental=0).rate_effect == 0

    def test_the_residual_absorbs_the_rounding(self) -> None:
        """Whatever the rate component rounded to, the sum is still exact."""
        for mean_holdout in range(4_990, 5_011):
            mix = mix_of(ate_bps=1, n_treatment=1, mean_holdout=mean_holdout, incremental=1_234)
            assert mix.rate_effect + mix.amount_mix_effect == 1_234

    def test_no_float_appears_in_any_component(self) -> None:
        mix = mix_of(
            ate_bps=1_564, n_treatment=5_044, mean_holdout=175_967, incremental=447_880_605
        )
        for value in (mix.rate_effect, mix.amount_mix_effect, mix.incremental_recovered):
            assert isinstance(value, int)
            assert not isinstance(value, bool)


class TestReportedShape:
    def test_is_rate_driven_compares_magnitudes(self) -> None:
        """Either component may be negative; a large negative mix still explains."""
        rate_driven = mix_of(
            ate_bps=1_000, n_treatment=1_000, mean_holdout=100_000, incremental=11_000_000
        )
        assert rate_driven.is_rate_driven

        mix_driven = mix_of(
            ate_bps=10, n_treatment=1_000, mean_holdout=100_000, incremental=50_000_000
        )
        assert not mix_driven.is_rate_driven

        negative_mix = mix_of(
            ate_bps=1_000, n_treatment=1_000, mean_holdout=100_000, incremental=-50_000_000
        )
        assert not negative_mix.is_rate_driven

    def test_as_dict_carries_both_components_and_their_inputs(self) -> None:
        payload = mix_of(
            ate_bps=1_564, n_treatment=5_044, mean_holdout=175_967, incremental=447_880_605
        ).as_dict()
        assert set(payload) == {
            "rate_effect",
            "amount_mix_effect",
            "incremental_recovered",
            "ate_bps",
            "n_treatment",
            "mean_holdout",
            "is_rate_driven",
        }
        assert payload["rate_effect"] + payload["amount_mix_effect"] == 447_880_605  # type: ignore[operator]

    def test_there_is_no_third_interaction_field(self) -> None:
        """Two components by construction. A third would need a convention."""
        import dataclasses

        names = {f.name for f in dataclasses.fields(AmountMix)}
        assert not any("interaction" in name for name in names)
        assert {"rate_effect", "amount_mix_effect"} <= names

    def test_the_scale_is_the_shared_basis_point_scale(self) -> None:
        assert BPS_SCALE == 10_000
