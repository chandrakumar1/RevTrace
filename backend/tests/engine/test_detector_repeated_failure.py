"""Repeated payment failure detector, with its negative controls.

The negative controls are not an afterthought here — a detector that fires on
S02 or S03 would report every customer who ever had a card declined as a
revenue leak.
"""

from __future__ import annotations

import pytest

from app.engine.detectors.config import DetectorConfig
from app.engine.detectors.repeated_failure import DETECTION_RULE, detect
from app.models.enums import RiskType
from tests.engine.conftest import AS_OF, order_timeline

#: Scenarios where revenue really was lost to repeated failure.
POSITIVE = ("S04", "S04b", "S04c", "S08", "S09", "S12", "S12b")

#: Scenarios that must produce NOTHING.
NEGATIVE = ("S01", "S02", "S03", "S05", "S07", "S10", "S11", "S13")


class TestPositiveDetection:
    @pytest.mark.parametrize("scenario", POSITIVE)
    def test_fires(self, scenario: str) -> None:
        assert len(detect(order_timeline(scenario), AS_OF)) == 1

    @pytest.mark.parametrize("scenario", POSITIVE)
    def test_reports_the_right_risk_type(self, scenario: str) -> None:
        finding = detect(order_timeline(scenario), AS_OF)[0]
        assert finding.risk_type == RiskType.REPEATED_PAYMENT_FAILURE.value

    def test_amount_is_the_order_amount_once(self) -> None:
        timeline = order_timeline("S04")
        finding = detect(timeline, AS_OF)[0]

        assert finding.amount_at_risk == timeline.amount_minor
        assert finding.amount_at_risk != sum(a.amount_minor for a in timeline.attempts)

    def test_records_the_rule_version(self) -> None:
        assert detect(order_timeline("S04"), AS_OF)[0].detection_rule == DETECTION_RULE

    def test_reason_names_the_failure_code(self) -> None:
        assert "card_declined" in detect(order_timeline("S04"), AS_OF)[0].reason

    def test_reason_counts_the_failures(self) -> None:
        assert detect(order_timeline("S04"), AS_OF)[0].reason.startswith("3 failed")

    def test_evidence_points_at_real_events(self) -> None:
        timeline = order_timeline("S04")
        finding = detect(timeline, AS_OF)[0]
        known = {e.external_event_id for e in timeline.events}

        assert finding.evidence_event_ids
        assert set(finding.evidence_event_ids) <= known

    def test_detected_at_is_the_supplied_instant(self) -> None:
        """No detector reads the clock."""
        assert detect(order_timeline("S04"), AS_OF)[0].detected_at == AS_OF

    def test_carries_order_and_customer(self) -> None:
        timeline = order_timeline("S04")
        finding = detect(timeline, AS_OF)[0]
        assert finding.order_id == timeline.order_id
        assert finding.customer_id == timeline.customer_id

    def test_timeout_failures_are_detected(self) -> None:
        """S04c times out rather than declining; still a leak."""
        finding = detect(order_timeline("S04c"), AS_OF)[0]
        assert "gateway_timeout" in finding.reason

    def test_high_value_reports_a_larger_amount(self) -> None:
        typical = detect(order_timeline("S04"), AS_OF)[0]
        high = detect(order_timeline("S04b"), AS_OF)[0]
        assert high.amount_at_risk > typical.amount_at_risk


class TestNegativeControls:
    """Nothing here is a revenue leak."""

    @pytest.mark.parametrize("scenario", NEGATIVE)
    def test_produces_nothing(self, scenario: str) -> None:
        assert detect(order_timeline(scenario), AS_OF) == ()

    def test_retry_success_is_not_a_leak(self) -> None:
        """S02: one failure, then an organic retry. Revenue was never lost."""
        timeline = order_timeline("S02")
        assert timeline.failed_attempts
        assert detect(timeline, AS_OF) == ()

    def test_multiple_attempts_then_success_is_not_a_leak(self) -> None:
        """S03: two failures before success clears the min-attempt threshold."""
        timeline = order_timeline("S03")
        assert len(timeline.failed_attempts) >= 2
        assert detect(timeline, AS_OF) == ()

    def test_recovery_success_is_not_a_standing_leak(self) -> None:
        """S11: failures, a recovery action, then payment."""
        timeline = order_timeline("S11")
        assert len(timeline.failed_attempts) >= 2
        assert detect(timeline, AS_OF) == ()

    def test_refund_is_not_a_leak(self) -> None:
        assert detect(order_timeline("S13"), AS_OF) == ()

    def test_captured_but_unreconciled_is_not_a_payment_failure(self) -> None:
        """S10 belongs to the reconciliation detector, not this one."""
        assert detect(order_timeline("S10"), AS_OF) == ()

    def test_abandonment_is_not_a_payment_failure(self) -> None:
        """S05 never attempted payment, so there is nothing to have failed."""
        assert detect(order_timeline("S05"), AS_OF) == ()

    def test_duplicate_delivery_alone_is_not_a_leak(self) -> None:
        assert detect(order_timeline("S07"), AS_OF) == ()

    def test_a_single_failure_is_not_enough(self) -> None:
        timeline = order_timeline("S02")
        assert len(timeline.failed_attempts) == 1
        assert detect(timeline, AS_OF, DetectorConfig(min_failed_attempts=2)) == ()


class TestDeliveryPathologyDoesNotChangeTheVerdict:
    """S08/S09/S12b must reach exactly the same conclusion as S04."""

    def test_out_of_order_matches_the_clean_case(self) -> None:
        clean = detect(order_timeline("S04"), AS_OF)[0]
        scrambled = detect(order_timeline("S08"), AS_OF)[0]
        assert scrambled.amount_at_risk == clean.amount_at_risk
        assert scrambled.confidence_bps == clean.confidence_bps

    def test_delayed_matches_the_clean_case(self) -> None:
        clean = detect(order_timeline("S04"), AS_OF)[0]
        delayed = detect(order_timeline("S09"), AS_OF)[0]
        assert delayed.amount_at_risk == clean.amount_at_risk
        assert delayed.confidence_bps == clean.confidence_bps

    def test_missing_event_still_detects(self) -> None:
        """Detection degrades gracefully rather than going silent."""
        finding = detect(order_timeline("S12b"), AS_OF)[0]
        assert finding.amount_at_risk == order_timeline("S04").amount_minor

    def test_missing_event_lowers_confidence_and_says_so(self) -> None:
        clean = detect(order_timeline("S04"), AS_OF)[0]
        with_gap = detect(order_timeline("S12b"), AS_OF)[0]

        assert with_gap.confidence_bps < clean.confidence_bps
        assert "never delivered" in with_gap.reason


class TestWindowing:
    def test_failures_outside_the_window_do_not_fire(self) -> None:
        """Two declines months apart are two purchases, not one struggle."""
        assert detect(order_timeline("S04"), AS_OF, DetectorConfig(failure_window_seconds=0)) == ()

    def test_clustered_failures_fire(self) -> None:
        assert detect(order_timeline("S04"), AS_OF, DetectorConfig(failure_window_seconds=86_400))

    def test_raising_the_threshold_suppresses(self) -> None:
        assert detect(order_timeline("S04"), AS_OF, DetectorConfig(min_failed_attempts=4)) == ()

    def test_lowering_the_threshold_still_respects_success(self) -> None:
        """Even at a threshold of 1, a paid order is not a leak."""
        assert detect(order_timeline("S02"), AS_OF, DetectorConfig(min_failed_attempts=1)) == ()


class TestDeterminism:
    @pytest.mark.parametrize("scenario", POSITIVE)
    def test_repeated_runs_are_identical(self, scenario: str) -> None:
        timeline = order_timeline(scenario)
        assert detect(timeline, AS_OF) == detect(timeline, AS_OF)

    def test_natural_key_is_stable(self) -> None:
        finding = detect(order_timeline("S04"), AS_OF)[0]
        assert finding.natural_key == (
            finding.merchant_id,
            finding.order_id,
            RiskType.REPEATED_PAYMENT_FAILURE.value,
        )


class TestAuthorityBoundary:
    def test_finding_carries_no_recommendation_or_approval(self) -> None:
        finding = detect(order_timeline("S04"), AS_OF)[0]
        for forbidden in (
            "recommended_action",
            "expected_recovery",
            "approved",
            "policy_status",
            "strategy",
        ):
            assert not hasattr(finding, forbidden)
