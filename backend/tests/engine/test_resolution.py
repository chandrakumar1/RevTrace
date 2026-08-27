"""Risk resolution and detection re-run idempotence.

Two properties carry this milestone:

* An open risk must **close** when the timeline moves on — a late capture or a
  successful recovery. A detector that fires and never retracts leaves a
  permanently wrong risk on the dashboard.
* Re-running detection must be **idempotent**: the same timeline replayed
  produces no second copy of the same risk.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.engine.detectors import detect_for_merchant, detect_for_order
from app.engine.detectors.config import DetectorConfig
from app.engine.resolution import (
    OPEN_STATUSES,
    TERMINAL_STATUSES,
    DetectionDelta,
    KnownRisk,
    RiskResolution,
    known_from_findings,
    reconcile_order,
    reconcile_subscription,
)
from app.models.enums import RiskStatus, RiskType
from app.services.tracing.reconstruction import reconstruct_merchant, reconstruct_order
from tests.engine.conftest import AS_OF, order_timeline, subscription_timeline
from tests.tracing.conftest import EPOCH, ORDER_ID, event, merchant_id_of, scenario_events

FAILING_EVENTS = [
    event("order.created", 0, order_ref="o1", amount_minor=5000, currency="INR"),
    event(
        "payment.attempted",
        30,
        payment_ref="p1",
        attempt_number=1,
        amount_minor=5000,
        currency="INR",
    ),
    event(
        "payment.failed",
        33,
        payment_ref="p1",
        attempt_number=1,
        amount_minor=5000,
        currency="INR",
        failure_code="card_declined",
    ),
    event(
        "payment.attempted",
        200,
        payment_ref="p2",
        attempt_number=2,
        amount_minor=5000,
        currency="INR",
    ),
    event(
        "payment.failed",
        203,
        payment_ref="p2",
        attempt_number=2,
        amount_minor=5000,
        currency="INR",
        failure_code="card_declined",
    ),
]

LATE_SUCCESS_EVENTS = [
    *FAILING_EVENTS,
    event(
        "payment.attempted",
        900,
        payment_ref="p3",
        attempt_number=3,
        amount_minor=5000,
        currency="INR",
    ),
    event(
        "payment.captured",
        910,
        payment_ref="p3",
        attempt_number=3,
        amount_minor=5000,
        currency="INR",
    ),
    event("order.paid", 915, order_ref="o1", amount_minor=5000, currency="INR"),
]

RECOVERY_EVENTS = [
    *FAILING_EVENTS,
    event("recovery.action_executed", 800, action_ref="a1", action_type="create_payment_link"),
    event(
        "payment.attempted",
        900,
        payment_ref="p3",
        attempt_number=3,
        amount_minor=5000,
        currency="INR",
    ),
    event(
        "payment.captured",
        910,
        payment_ref="p3",
        attempt_number=3,
        amount_minor=5000,
        currency="INR",
    ),
    event("recovery.succeeded", 912, action_ref="a1"),
    event("order.paid", 915, order_ref="o1", amount_minor=5000, currency="INR"),
]


def _open_risk_from(timeline) -> tuple[KnownRisk, ...]:
    """The risks a first detection run would have recorded."""
    return known_from_findings(detect_for_order(timeline, AS_OF))


class TestFirstRun:
    def test_new_risk_is_reported_as_new(self) -> None:
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        delta = reconcile_order(timeline, [], AS_OF)

        assert len(delta.new_findings) == 1
        assert delta.unchanged_keys == ()
        assert delta.resolutions == ()

    def test_clean_scenario_produces_an_empty_delta(self) -> None:
        delta = reconcile_order(order_timeline("S01"), [], AS_OF)
        assert delta == DetectionDelta()

    def test_new_finding_carries_the_expected_risk_type(self) -> None:
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        finding = reconcile_order(timeline, [], AS_OF).new_findings[0]
        assert finding.risk_type == RiskType.REPEATED_PAYMENT_FAILURE.value


class TestReRunIdempotence:
    """The same timeline replayed must not produce a second copy."""

    def test_second_run_reports_nothing_new(self) -> None:
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        known = _open_risk_from(timeline)

        delta = reconcile_order(timeline, known, AS_OF)
        assert delta.new_findings == ()
        assert len(delta.unchanged_keys) == 1
        assert delta.resolutions == ()

    def test_ten_runs_stay_stable(self) -> None:
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        known = _open_risk_from(timeline)

        for _ in range(10):
            delta = reconcile_order(timeline, known, AS_OF)
            assert delta.new_findings == ()
            assert len(delta.unchanged_keys) == 1

    @pytest.mark.parametrize("scenario", ["S04", "S05", "S08", "S09", "S12", "S12b", "S10"])
    def test_scenarios_are_idempotent(self, scenario: str) -> None:
        timeline = order_timeline(scenario)
        known = _open_risk_from(timeline)

        delta = reconcile_order(timeline, known, AS_OF)
        assert delta.new_findings == ()
        assert delta.resolutions == ()

    def test_natural_key_is_what_makes_it_idempotent(self) -> None:
        timeline = order_timeline("S04")
        first = detect_for_order(timeline, AS_OF)
        second = detect_for_order(timeline, AS_OF)

        assert [f.natural_key for f in first] == [f.natural_key for f in second]

    def test_merchant_level_rerun_is_idempotent(self) -> None:
        timeline = reconstruct_merchant(merchant_id_of("S14"), scenario_events("S14"))
        first = detect_for_merchant(timeline, AS_OF)
        second = detect_for_merchant(timeline, AS_OF)

        assert [f.natural_key for f in first] == [f.natural_key for f in second]


class TestLateSuccessResolution:
    """Scenario D4 — the failure mode that makes a detector untrustworthy."""

    def test_late_capture_closes_the_risk(self) -> None:
        known = _open_risk_from(reconstruct_order(ORDER_ID, FAILING_EVENTS))
        extended = reconstruct_order(ORDER_ID, LATE_SUCCESS_EVENTS)

        delta = reconcile_order(extended, known, AS_OF)
        assert len(delta.resolutions) == 1
        assert delta.resolutions[0].new_status == RiskStatus.RECOVERED.value

    def test_resolution_records_the_recovered_amount(self) -> None:
        known = _open_risk_from(reconstruct_order(ORDER_ID, FAILING_EVENTS))
        extended = reconstruct_order(ORDER_ID, LATE_SUCCESS_EVENTS)

        assert reconcile_order(extended, known, AS_OF).total_recovered == 5000

    def test_resolution_reason_says_it_was_organic(self) -> None:
        known = _open_risk_from(reconstruct_order(ORDER_ID, FAILING_EVENTS))
        extended = reconstruct_order(ORDER_ID, LATE_SUCCESS_EVENTS)

        reason = reconcile_order(extended, known, AS_OF).resolutions[0].reason
        assert "without intervention" in reason

    def test_no_new_risk_is_raised_alongside(self) -> None:
        known = _open_risk_from(reconstruct_order(ORDER_ID, FAILING_EVENTS))
        extended = reconstruct_order(ORDER_ID, LATE_SUCCESS_EVENTS)

        assert reconcile_order(extended, known, AS_OF).new_findings == ()

    def test_resolution_is_itself_idempotent(self) -> None:
        """Resolving twice must not double-count the recovery."""
        known = _open_risk_from(reconstruct_order(ORDER_ID, FAILING_EVENTS))
        extended = reconstruct_order(ORDER_ID, LATE_SUCCESS_EVENTS)

        first = reconcile_order(extended, known, AS_OF)
        resolved = KnownRisk(
            merchant_id=known[0].merchant_id,
            order_id=known[0].order_id,
            risk_type=known[0].risk_type,
            status=first.resolutions[0].new_status,
            amount_at_risk=known[0].amount_at_risk,
            detected_at=known[0].detected_at,
        )

        second = reconcile_order(extended, [resolved], AS_OF)
        assert second.resolutions == ()
        assert second.total_recovered == 0


class TestRecoverySuccessResolution:
    def test_recovery_success_closes_the_risk(self) -> None:
        known = _open_risk_from(reconstruct_order(ORDER_ID, FAILING_EVENTS))
        extended = reconstruct_order(ORDER_ID, RECOVERY_EVENTS)

        delta = reconcile_order(extended, known, AS_OF)
        assert delta.resolutions[0].new_status == RiskStatus.RECOVERED.value

    def test_reason_names_the_recovery_action(self) -> None:
        known = _open_risk_from(reconstruct_order(ORDER_ID, FAILING_EVENTS))
        extended = reconstruct_order(ORDER_ID, RECOVERY_EVENTS)

        reason = reconcile_order(extended, known, AS_OF).resolutions[0].reason
        assert "recovery action was executed" in reason

    def test_recovered_amount_is_the_captured_amount(self) -> None:
        known = _open_risk_from(reconstruct_order(ORDER_ID, FAILING_EVENTS))
        extended = reconstruct_order(ORDER_ID, RECOVERY_EVENTS)

        assert reconcile_order(extended, known, AS_OF).total_recovered == 5000

    def test_recovery_without_capture_does_not_recover(self) -> None:
        """S12: an action was executed and no money followed."""
        timeline = order_timeline("S12")
        known = _open_risk_from(timeline)

        delta = reconcile_order(timeline, known, AS_OF)
        assert delta.resolutions == ()
        assert delta.total_recovered == 0

    def test_real_recovery_scenario_never_opens_a_risk(self) -> None:
        """S11 in full is silent from the start."""
        assert reconcile_order(order_timeline("S11"), [], AS_OF) == DetectionDelta()


class TestReconciliationResolution:
    def test_late_order_paid_closes_the_anomaly(self) -> None:
        known = _open_risk_from(order_timeline("S10"))
        assert known

        events = [*scenario_events("S10")]
        events.append(
            event("order.paid", 100_000, order_ref="o1", amount_minor=5000, currency="INR")
        )
        extended = reconstruct_order(known[0].order_id, events)  # type: ignore[arg-type]

        delta = reconcile_order(extended, known, AS_OF)
        assert delta.resolutions[0].new_status == RiskStatus.RECOVERED.value

    def test_reconciliation_recovery_moves_no_money(self) -> None:
        """The funds had already arrived; nothing is newly recovered."""
        known = _open_risk_from(order_timeline("S10"))
        events = [*scenario_events("S10")]
        events.append(
            event("order.paid", 100_000, order_ref="o1", amount_minor=5000, currency="INR")
        )
        extended = reconstruct_order(known[0].order_id, events)  # type: ignore[arg-type]

        assert reconcile_order(extended, known, AS_OF).total_recovered == 0

    def test_reason_says_the_event_was_late_not_missing(self) -> None:
        known = _open_risk_from(order_timeline("S10"))
        events = [*scenario_events("S10")]
        events.append(
            event("order.paid", 100_000, order_ref="o1", amount_minor=5000, currency="INR")
        )
        extended = reconstruct_order(known[0].order_id, events)  # type: ignore[arg-type]

        assert "late, not missing" in reconcile_order(extended, known, AS_OF).resolutions[0].reason


class TestFalsePositiveResolution:
    def test_retracted_evidence_becomes_a_false_positive(self) -> None:
        """The risk stopped firing and no money was collected."""
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        known = _open_risk_from(timeline)

        # Raising the threshold makes the detector stop firing.
        delta = reconcile_order(timeline, known, AS_OF, DetectorConfig(min_failed_attempts=5))

        assert delta.resolutions[0].new_status == RiskStatus.FALSE_POSITIVE.value
        assert delta.total_recovered == 0

    def test_false_positive_reason_is_explicit(self) -> None:
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        known = _open_risk_from(timeline)
        delta = reconcile_order(timeline, known, AS_OF, DetectorConfig(min_failed_attempts=5))

        assert "did not hold" in delta.resolutions[0].reason


class TestExpiry:
    def test_an_old_unresolved_risk_expires(self) -> None:
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        known = (
            KnownRisk(
                merchant_id=timeline.merchant_id,
                order_id=timeline.order_id,
                risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value,
                status=RiskStatus.DETECTED.value,
                amount_at_risk=5000,
                detected_at=EPOCH,
            ),
        )
        delta = reconcile_order(timeline, known, EPOCH + timedelta(days=45))

        assert delta.resolutions[0].new_status == RiskStatus.EXPIRED.value

    def test_a_recent_risk_does_not_expire(self) -> None:
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        known = _open_risk_from(timeline)

        delta = reconcile_order(timeline, known, EPOCH + timedelta(days=1))
        assert delta.resolutions == ()
        assert len(delta.unchanged_keys) == 1

    def test_expiry_recovers_nothing(self) -> None:
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        known = (
            KnownRisk(
                merchant_id=timeline.merchant_id,
                order_id=timeline.order_id,
                risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value,
                status=RiskStatus.DETECTED.value,
                amount_at_risk=5000,
                detected_at=EPOCH,
            ),
        )
        delta = reconcile_order(timeline, known, EPOCH + timedelta(days=45))
        assert delta.total_recovered == 0

    def test_expiry_window_is_configurable(self) -> None:
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        known = (
            KnownRisk(
                merchant_id=timeline.merchant_id,
                order_id=timeline.order_id,
                risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value,
                status=RiskStatus.DETECTED.value,
                amount_at_risk=5000,
                detected_at=EPOCH,
            ),
        )
        config = DetectorConfig(risk_expiry_seconds=10)
        delta = reconcile_order(timeline, known, EPOCH + timedelta(seconds=11), config)
        assert delta.resolutions[0].new_status == RiskStatus.EXPIRED.value


class TestTerminalRisksStayClosed:
    """A resolved risk is never silently reopened."""

    @pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
    def test_terminal_risk_is_left_alone(self, status: str) -> None:
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        known = (
            KnownRisk(
                merchant_id=timeline.merchant_id,
                order_id=timeline.order_id,
                risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value,
                status=status,
                amount_at_risk=5000,
                detected_at=AS_OF,
            ),
        )
        delta = reconcile_order(timeline, known, AS_OF)

        assert delta.resolutions == ()
        assert delta.unchanged_keys == ()

    @pytest.mark.parametrize("status", sorted(OPEN_STATUSES))
    def test_open_risk_is_considered(self, status: str) -> None:
        timeline = reconstruct_order(ORDER_ID, FAILING_EVENTS)
        known = (
            KnownRisk(
                merchant_id=timeline.merchant_id,
                order_id=timeline.order_id,
                risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value,
                status=status,
                amount_at_risk=5000,
                detected_at=AS_OF,
            ),
        )
        assert reconcile_order(timeline, known, AS_OF).unchanged_keys


class TestSubscriptionResolution:
    def test_resumed_billing_closes_the_risk(self) -> None:
        from app.services.tracing.reconstruction import reconstruct_subscription

        failing = subscription_timeline("S06")
        known = known_from_findings(
            detect_for_merchant(
                reconstruct_merchant(merchant_id_of("S06"), scenario_events("S06")), AS_OF
            )
        )
        assert known

        resumed = reconstruct_subscription(
            failing.subscription_ref,
            [
                *failing.events,
                event(
                    "subscription.charged",
                    20_000_000,
                    order_id=None,
                    subscription_ref=failing.subscription_ref,
                    amount_minor=29_900,
                    currency="INR",
                    cycle=6,
                ),
            ],
        )
        delta = reconcile_subscription(resumed, known, AS_OF)

        assert delta.resolutions[0].new_status == RiskStatus.RECOVERED.value
        assert "resumed" in delta.resolutions[0].reason

    def test_still_failing_subscription_stays_open(self) -> None:
        subscription = subscription_timeline("S06")
        known = known_from_findings(
            detect_for_merchant(
                reconstruct_merchant(merchant_id_of("S06"), scenario_events("S06")), AS_OF
            )
        )

        delta = reconcile_subscription(subscription, known, AS_OF)
        assert delta.resolutions == ()
        assert len(delta.unchanged_keys) == 1


class TestValidation:
    def test_invalid_risk_type_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid risk_type"):
            KnownRisk(
                merchant_id=ORDER_ID,
                order_id=ORDER_ID,
                risk_type="not_a_risk",
                status=RiskStatus.DETECTED.value,
                amount_at_risk=0,
                detected_at=AS_OF,
            )

    def test_invalid_status_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid status"):
            KnownRisk(
                merchant_id=ORDER_ID,
                order_id=ORDER_ID,
                risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value,
                status="vibes",
                amount_at_risk=0,
                detected_at=AS_OF,
            )

    def test_float_recovered_amount_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="integer count of minor units"):
            RiskResolution(
                natural_key=(ORDER_ID, ORDER_ID, RiskType.REPEATED_PAYMENT_FAILURE.value),
                previous_status=RiskStatus.DETECTED.value,
                new_status=RiskStatus.RECOVERED.value,
                reason="x",
                resolved_at=AS_OF,
                amount_recovered=100.5,  # type: ignore[arg-type]
            )

    def test_negative_recovered_amount_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            RiskResolution(
                natural_key=(ORDER_ID, ORDER_ID, RiskType.REPEATED_PAYMENT_FAILURE.value),
                previous_status=RiskStatus.DETECTED.value,
                new_status=RiskStatus.RECOVERED.value,
                reason="x",
                resolved_at=AS_OF,
                amount_recovered=-1,
            )


class TestDetectorBehaviourPreserved:
    """M5 behaviour must be untouched by resolution."""

    @pytest.mark.parametrize("scenario", ["S01", "S02", "S03", "S07", "S11", "S13"])
    def test_negative_controls_still_silent(self, scenario: str) -> None:
        assert detect_for_order(order_timeline(scenario), AS_OF) == ()

    @pytest.mark.parametrize(
        "scenario", ["S04", "S04b", "S04c", "S05", "S08", "S09", "S12", "S12b"]
    )
    def test_leaks_still_detected(self, scenario: str) -> None:
        assert len(detect_for_order(order_timeline(scenario), AS_OF)) == 1
