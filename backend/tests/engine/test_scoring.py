"""Confidence scoring.

Two properties matter more than the specific numbers: the score is exactly
reproducible, and it is honestly labelled as a heuristic rather than a
probability.
"""

from __future__ import annotations

import pytest

from app.engine.scoring import (
    BASE_CHECKOUT_ABANDONMENT_BPS,
    BASE_RECONCILIATION_BPS,
    BASE_REPEATED_FAILURE_BPS,
    EXPLICIT_ABANDONMENT_BONUS_BPS,
    MAX_CONFIDENCE_BPS,
    MIN_CONFIDENCE_BPS,
    PER_INFERRED_GAP_PENALTY_BPS,
    SUBSCRIPTION_HALTED_BONUS_BPS,
    ConfidenceBreakdown,
    confidence_checkout_abandonment,
    confidence_reconciliation,
    confidence_repeated_failure,
    confidence_subscription_failure,
    explain_checkout_abandonment,
    explain_reconciliation,
    explain_repeated_failure,
    explain_subscription_failure,
)
from tests.engine.conftest import order_timeline, subscription_timeline

SCORED_SCENARIOS = ("S04", "S04b", "S04c", "S05", "S08", "S09", "S10", "S12", "S12b")


class TestDeterminism:
    @pytest.mark.parametrize("scenario", SCORED_SCENARIOS)
    def test_repeated_scoring_is_identical(self, scenario: str) -> None:
        timeline = order_timeline(scenario)
        scores = {confidence_repeated_failure(timeline) for _ in range(20)}
        assert len(scores) == 1

    def test_rebuilt_timeline_scores_the_same(self) -> None:
        assert confidence_repeated_failure(order_timeline("S04")) == (
            confidence_repeated_failure(order_timeline("S04"))
        )

    def test_breakdown_is_reproducible(self) -> None:
        timeline = order_timeline("S04")
        assert explain_repeated_failure(timeline) == explain_repeated_failure(timeline)

    def test_delivery_pathology_does_not_change_the_score(self) -> None:
        """S08 and S09 are S04 delivered badly. Confidence must not move."""
        baseline = confidence_repeated_failure(order_timeline("S04"))
        assert confidence_repeated_failure(order_timeline("S08")) == baseline
        assert confidence_repeated_failure(order_timeline("S09")) == baseline


class TestBounds:
    @pytest.mark.parametrize("scenario", SCORED_SCENARIOS)
    def test_within_basis_point_range(self, scenario: str) -> None:
        timeline = order_timeline(scenario)
        for score in (
            confidence_repeated_failure(timeline),
            confidence_checkout_abandonment(timeline),
            confidence_reconciliation(timeline),
        ):
            assert MIN_CONFIDENCE_BPS <= score <= MAX_CONFIDENCE_BPS

    @pytest.mark.parametrize("scenario", SCORED_SCENARIOS)
    def test_score_is_an_integer(self, scenario: str) -> None:
        score = confidence_repeated_failure(order_timeline(scenario))
        assert isinstance(score, int) and not isinstance(score, bool)

    def test_subscription_score_in_range(self) -> None:
        score = confidence_subscription_failure(subscription_timeline("S06"))
        assert MIN_CONFIDENCE_BPS <= score <= MAX_CONFIDENCE_BPS

    def test_breakdown_rejects_an_out_of_range_total(self) -> None:
        with pytest.raises(ValueError, match="outside"):
            ConfidenceBreakdown(risk_type="x", total_bps=10_001)

    def test_breakdown_rejects_a_float_total(self) -> None:
        with pytest.raises(TypeError, match="integer"):
            ConfidenceBreakdown(risk_type="x", total_bps=5000.5)  # type: ignore[arg-type]


class TestEvidenceStrength:
    """More corroborating evidence raises the score; missing evidence lowers it."""

    def test_three_failures_beat_two(self) -> None:
        three = confidence_repeated_failure(order_timeline("S04"))
        two = confidence_repeated_failure(order_timeline("S12"))
        assert three > two

    def test_extra_failure_bonus_is_applied(self) -> None:
        breakdown = explain_repeated_failure(order_timeline("S04"))
        assert dict(breakdown.components)["corroborating_failures"] > 0

    def test_two_failures_get_no_bonus(self) -> None:
        breakdown = explain_repeated_failure(order_timeline("S12"))
        assert "corroborating_failures" not in dict(breakdown.components)

    def test_missing_evidence_lowers_the_score(self) -> None:
        """S12b is S04 with one event dropped. Same conclusion, less certainty."""
        complete = confidence_repeated_failure(order_timeline("S04"))
        with_gap = confidence_repeated_failure(order_timeline("S12b"))
        assert with_gap < complete

    def test_missing_evidence_penalty_is_recorded(self) -> None:
        breakdown = explain_repeated_failure(order_timeline("S12b"))
        assert dict(breakdown.components)["missing_evidence"] == -PER_INFERRED_GAP_PENALTY_BPS

    def test_a_gap_never_suppresses_the_finding(self) -> None:
        assert confidence_repeated_failure(order_timeline("S12b")) > MIN_CONFIDENCE_BPS

    def test_explicit_abandonment_beats_inferred_silence(self) -> None:
        breakdown = explain_checkout_abandonment(order_timeline("S05"))
        assert dict(breakdown.components)["explicit_abandonment_event"] == (
            EXPLICIT_ABANDONMENT_BONUS_BPS
        )

    def test_halted_subscription_raises_confidence(self) -> None:
        breakdown = explain_subscription_failure(subscription_timeline("S06"))
        assert dict(breakdown.components)["subscription_halted"] == SUBSCRIPTION_HALTED_BONUS_BPS


class TestAmountIndependence:
    """A larger order is not stronger evidence."""

    def test_high_value_scores_the_same_as_typical(self) -> None:
        typical = order_timeline("S04")
        high_value = order_timeline("S04b")

        assert high_value.amount_minor > typical.amount_minor
        assert confidence_repeated_failure(high_value) == confidence_repeated_failure(typical)

    def test_amount_is_absent_from_the_breakdown(self) -> None:
        component_names = dict(explain_repeated_failure(order_timeline("S04b")).components)
        assert not any("amount" in name for name in component_names)


class TestArithmeticIsCheckable:
    """`explain()` must actually sum to the reported total."""

    @pytest.mark.parametrize("scenario", SCORED_SCENARIOS)
    def test_components_sum_to_the_total(self, scenario: str) -> None:
        breakdown = explain_repeated_failure(order_timeline(scenario))
        assert sum(weight for _, weight in breakdown.components) == breakdown.total_bps

    def test_base_is_always_present(self) -> None:
        assert dict(explain_repeated_failure(order_timeline("S04")).components)["base"] == (
            BASE_REPEATED_FAILURE_BPS
        )

    def test_abandonment_base(self) -> None:
        assert dict(explain_checkout_abandonment(order_timeline("S05")).components)["base"] == (
            BASE_CHECKOUT_ABANDONMENT_BPS
        )

    def test_reconciliation_base(self) -> None:
        assert dict(explain_reconciliation(order_timeline("S10")).components)["base"] == (
            BASE_RECONCILIATION_BPS
        )

    def test_all_components_are_integers(self) -> None:
        for _, weight in explain_repeated_failure(order_timeline("S04")).components:
            assert isinstance(weight, int) and not isinstance(weight, bool)


class TestHonestLabelling:
    """It is a heuristic, and it must keep saying so."""

    def test_breakdown_declares_itself_synthetic(self) -> None:
        assert explain_repeated_failure(order_timeline("S04")).is_synthetic_heuristic is True

    @pytest.mark.parametrize(
        "explain",
        [explain_repeated_failure, explain_checkout_abandonment, explain_reconciliation],
    )
    def test_every_order_scorer_declares_itself(self, explain: object) -> None:
        breakdown = explain(order_timeline("S04"))  # type: ignore[operator]
        assert breakdown.is_synthetic_heuristic is True

    def test_subscription_scorer_declares_itself(self) -> None:
        assert (
            explain_subscription_failure(subscription_timeline("S06")).is_synthetic_heuristic
            is True
        )

    def test_module_documents_it_is_not_a_probability(self) -> None:
        import app.engine.scoring as scoring

        assert scoring.__doc__ is not None
        assert "not a validated probability" in scoring.__doc__

    def test_risk_type_is_recorded(self) -> None:
        assert explain_repeated_failure(order_timeline("S04")).risk_type == (
            "repeated_payment_failure"
        )
