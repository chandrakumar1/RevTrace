"""Subscription payment failure detector, with its negative controls.

The guard that matters: a subscription that failed and then recovered must be
silent. `trailing_failure_streak` is what enforces it.
"""

from __future__ import annotations

import uuid

import pytest

from app.engine.detectors.config import DetectorConfig
from app.engine.detectors.subscription import DETECTION_RULE, detect
from app.models.enums import RiskType
from app.services.tracing.reconstruction import reconstruct_subscription
from tests.engine.conftest import AS_OF, subscription_timeline
from tests.tracing.conftest import event

SUB_REF = "sub_1"


def _sub_event(event_type: str, occurred: int, *, amount: int = 29_900, cycle: int = 1):
    """A subscription event: no order_id, carries a subscription_ref."""
    return event(
        event_type,
        occurred,
        order_id=None,
        subscription_ref=SUB_REF,
        amount_minor=amount,
        currency="INR",
        cycle=cycle,
        external_id=f"evt_{event_type}_{occurred}",
    )


class TestPositiveDetection:
    def test_fires_on_halted_subscription(self) -> None:
        assert len(detect(subscription_timeline("S06"), AS_OF)) == 1

    def test_reports_the_right_risk_type(self) -> None:
        finding = detect(subscription_timeline("S06"), AS_OF)[0]
        assert finding.risk_type == RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value

    def test_amount_sums_the_failed_cycles(self) -> None:
        """A sum is correct here: each cycle is a separate lost charge."""
        subscription = subscription_timeline("S06")
        finding = detect(subscription, AS_OF)[0]

        assert finding.amount_at_risk == subscription.failed_amount_minor
        assert finding.amount_at_risk > 0

    def test_records_the_rule_version(self) -> None:
        assert detect(subscription_timeline("S06"), AS_OF)[0].detection_rule == DETECTION_RULE

    def test_carries_no_order_id(self) -> None:
        """Subscription events have no order in the Phase 1 schema."""
        assert detect(subscription_timeline("S06"), AS_OF)[0].order_id is None

    def test_subscription_ref_is_reported(self) -> None:
        finding = detect(subscription_timeline("S06"), AS_OF)[0]
        assert finding.order_ref == subscription_timeline("S06").subscription_ref

    def test_reason_counts_the_failures(self) -> None:
        assert detect(subscription_timeline("S06"), AS_OF)[0].reason.startswith("2 consecutive")

    def test_halt_is_named_in_the_reason(self) -> None:
        assert "halted" in detect(subscription_timeline("S06"), AS_OF)[0].reason

    def test_evidence_points_at_failure_events(self) -> None:
        subscription = subscription_timeline("S06")
        finding = detect(subscription, AS_OF)[0]
        known = {e.external_event_id for e in subscription.events}

        assert finding.evidence_event_ids
        assert set(finding.evidence_event_ids) <= known

    def test_fires_without_a_halt_event(self) -> None:
        subscription = reconstruct_subscription(
            SUB_REF,
            [
                _sub_event("subscription.charged", 0, cycle=1),
                _sub_event("subscription.payment_failed", 100, cycle=2),
                _sub_event("subscription.payment_failed", 200, cycle=3),
            ],
        )
        assert len(detect(subscription, AS_OF)) == 1


class TestNegativeControls:
    def test_healthy_subscription_is_silent(self) -> None:
        subscription = reconstruct_subscription(
            SUB_REF,
            [
                _sub_event("subscription.charged", 0, cycle=1),
                _sub_event("subscription.charged", 100, cycle=2),
                _sub_event("subscription.charged", 200, cycle=3),
            ],
        )
        assert detect(subscription, AS_OF) == ()

    def test_recovered_subscription_is_silent(self) -> None:
        """Two failures, then a successful charge. Revenue resumed."""
        subscription = reconstruct_subscription(
            SUB_REF,
            [
                _sub_event("subscription.payment_failed", 0, cycle=1),
                _sub_event("subscription.payment_failed", 100, cycle=2),
                _sub_event("subscription.charged", 200, cycle=3),
            ],
        )
        assert subscription.failed_cycles == 2
        assert subscription.trailing_failure_streak == 0
        assert detect(subscription, AS_OF) == ()

    def test_a_single_failure_is_not_enough(self) -> None:
        subscription = reconstruct_subscription(
            SUB_REF,
            [
                _sub_event("subscription.charged", 0, cycle=1),
                _sub_event("subscription.payment_failed", 100, cycle=2),
            ],
        )
        assert detect(subscription, AS_OF) == ()

    def test_interrupted_streak_is_silent(self) -> None:
        """fail, success, fail — no consecutive run reaches the threshold."""
        subscription = reconstruct_subscription(
            SUB_REF,
            [
                _sub_event("subscription.payment_failed", 0, cycle=1),
                _sub_event("subscription.charged", 100, cycle=2),
                _sub_event("subscription.payment_failed", 200, cycle=3),
            ],
        )
        assert subscription.failed_cycles == 2
        assert subscription.trailing_failure_streak == 1
        assert detect(subscription, AS_OF) == ()

    def test_zero_amount_does_not_fire(self) -> None:
        subscription = reconstruct_subscription(
            SUB_REF,
            [
                _sub_event("subscription.payment_failed", 0, amount=0, cycle=1),
                _sub_event("subscription.payment_failed", 100, amount=0, cycle=2),
            ],
        )
        assert detect(subscription, AS_OF) == ()

    def test_raising_the_threshold_suppresses(self) -> None:
        assert (
            detect(subscription_timeline("S06"), AS_OF, DetectorConfig(min_subscription_failures=5))
            == ()
        )


class TestDeterminism:
    def test_repeated_runs_are_identical(self) -> None:
        subscription = subscription_timeline("S06")
        assert detect(subscription, AS_OF) == detect(subscription, AS_OF)

    def test_detected_at_is_the_supplied_instant(self) -> None:
        assert detect(subscription_timeline("S06"), AS_OF)[0].detected_at == AS_OF

    def test_natural_key_has_no_order(self) -> None:
        finding = detect(subscription_timeline("S06"), AS_OF)[0]
        merchant, order, risk_type = finding.natural_key

        assert isinstance(merchant, uuid.UUID)
        assert order is None
        assert risk_type == RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value


class TestIntegerDiscipline:
    def test_amount_is_an_integer(self) -> None:
        amount = detect(subscription_timeline("S06"), AS_OF)[0].amount_at_risk
        assert isinstance(amount, int) and not isinstance(amount, bool)

    @pytest.mark.parametrize("cycles", [2, 3, 4])
    def test_amount_scales_with_failed_cycles(self, cycles: int) -> None:
        events = [
            _sub_event("subscription.payment_failed", i * 100, cycle=i) for i in range(cycles)
        ]
        subscription = reconstruct_subscription(SUB_REF, events)
        assert detect(subscription, AS_OF)[0].amount_at_risk == 29_900 * cycles
