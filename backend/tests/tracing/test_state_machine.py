"""Derived order state.

State comes from events, never from `orders.status`. The separation between
`state` and the `has_*` booleans is what makes the reconciliation anomaly
visible: an order can hold a captured payment without ever reaching `paid`.
"""

from __future__ import annotations

import pytest

from app.models.enums import OrderStatus
from app.services.tracing.reconstruction import reconstruct_order
from app.services.tracing.state import dominant_state
from tests.tracing.conftest import ORDER_ID, event, only_order


class TestStatePrecedence:
    def test_empty_defaults_to_created(self) -> None:
        assert dominant_state([]) == OrderStatus.CREATED.value

    def test_attempted_beats_created(self) -> None:
        assert dominant_state(["created", "attempted"]) == "attempted"

    def test_paid_beats_attempted(self) -> None:
        assert dominant_state(["attempted", "paid"]) == "paid"

    def test_refunded_beats_paid(self) -> None:
        assert dominant_state(["paid", "refunded"]) == "refunded"

    def test_precedence_ignores_input_order(self) -> None:
        """Terminal states are sticky regardless of arrival sequence."""
        assert dominant_state(["paid", "attempted", "created"]) == "paid"
        assert dominant_state(["created", "attempted", "paid"]) == "paid"

    def test_abandoned_beats_attempted(self) -> None:
        assert dominant_state(["attempted", "abandoned"]) == "abandoned"

    def test_unknown_state_does_not_win(self) -> None:
        assert dominant_state(["paid", "who_knows"]) == "paid"


class TestScenarioStates:
    @pytest.mark.parametrize(
        ("scenario", "expected"),
        [
            ("S01", OrderStatus.PAID.value),
            ("S02", OrderStatus.PAID.value),
            ("S03", OrderStatus.PAID.value),
            ("S04", OrderStatus.ATTEMPTED.value),
            ("S05", OrderStatus.ABANDONED.value),
            ("S10", OrderStatus.ATTEMPTED.value),
            ("S11", OrderStatus.PAID.value),
            ("S12", OrderStatus.ATTEMPTED.value),
            ("S13", OrderStatus.REFUNDED.value),
        ],
    )
    def test_derived_state(self, scenario: str, expected: str) -> None:
        assert only_order(scenario).state == expected


class TestTerminalSuccess:
    """The single most important suppression signal in detection."""

    @pytest.mark.parametrize("scenario", ["S01", "S02", "S03", "S11", "S13"])
    def test_successful_scenarios_reached_terminal_success(self, scenario: str) -> None:
        assert only_order(scenario).reached_terminal_success

    @pytest.mark.parametrize("scenario", ["S04", "S05", "S08", "S09", "S12", "S12b"])
    def test_unsuccessful_scenarios_did_not(self, scenario: str) -> None:
        assert not only_order(scenario).reached_terminal_success

    def test_success_after_failures_still_counts(self) -> None:
        """S02: one failure then an organic retry. Revenue was never lost."""
        timeline = only_order("S02")
        assert timeline.failed_attempts
        assert timeline.reached_terminal_success

    def test_capture_alone_counts_as_success(self) -> None:
        """S10 captured money but never reconciled; the money still arrived."""
        timeline = only_order("S10")
        assert timeline.has_capture
        assert not timeline.has_order_paid
        assert timeline.reached_terminal_success


class TestReconciliationVisibility:
    def test_captured_without_order_paid_is_distinguishable(self) -> None:
        timeline = only_order("S10")
        assert timeline.has_capture is True
        assert timeline.has_order_paid is False

    def test_healthy_payment_has_both(self) -> None:
        timeline = only_order("S01")
        assert timeline.has_capture and timeline.has_order_paid

    def test_failed_order_has_neither(self) -> None:
        timeline = only_order("S04")
        assert not timeline.has_capture and not timeline.has_order_paid


class TestCheckoutAndRefundFlags:
    def test_abandonment_flags(self) -> None:
        timeline = only_order("S05")
        assert timeline.has_checkout_started
        assert timeline.has_checkout_abandoned
        assert timeline.attempts == ()

    def test_refund_flag(self) -> None:
        timeline = only_order("S13")
        assert timeline.has_refund
        assert timeline.has_capture

    def test_no_spurious_checkout_flags(self) -> None:
        timeline = only_order("S04")
        assert not timeline.has_checkout_abandoned
        assert not timeline.has_refund


class TestRecoveryFlags:
    """Historical recovery events only — no case or approval is implied."""

    def test_recovery_success_flags(self) -> None:
        timeline = only_order("S11")
        assert timeline.has_recovery_action
        assert timeline.has_recovery_succeeded
        assert not timeline.has_recovery_failed

    def test_recovery_failure_flags(self) -> None:
        timeline = only_order("S12")
        assert timeline.has_recovery_action
        assert timeline.has_recovery_failed
        assert not timeline.has_recovery_succeeded

    def test_no_recovery_flags_on_ordinary_orders(self) -> None:
        timeline = only_order("S04")
        assert not timeline.has_recovery_action


class TestOrderAmount:
    def test_amount_is_an_integer(self) -> None:
        amount = only_order("S04").amount_minor
        assert isinstance(amount, int) and not isinstance(amount, bool)

    def test_amount_matches_the_order(self) -> None:
        from tests.tracing.conftest import scenario_result

        result = scenario_result("S04")
        assert only_order("S04").amount_minor == result.entities.orders[0].amount

    def test_float_amount_in_payload_is_ignored_not_coerced(self) -> None:
        """A float must never become a money value (ADR 0001)."""
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event("order.created", 0, amount_minor=1000, currency="INR"),
                event("payment.attempted", 30, amount_minor=99.5, currency="INR"),
            ],
        )
        assert timeline.amount_minor == 1000

    def test_captured_amount_is_zero_when_nothing_captured(self) -> None:
        assert only_order("S04").captured_amount_minor == 0

    def test_captured_amount_is_positive_when_paid(self) -> None:
        assert only_order("S01").captured_amount_minor > 0
