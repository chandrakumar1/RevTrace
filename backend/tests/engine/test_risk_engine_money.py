"""Money computations.

The headline test in this file is `TestNoAttemptSumInflation`: three failed
attempts on one order must put ONE order amount at risk. Summing the attempt
ledger is the natural mistake and it would silently triple the most visible
number in the product.
"""

from __future__ import annotations

import pytest

from app.core.money import CurrencyMismatchError
from app.engine.risk_engine import (
    MoneyBreakdown,
    amount_at_risk_checkout_abandonment,
    amount_at_risk_reconciliation,
    amount_at_risk_repeated_failure,
    amount_at_risk_subscription,
    captured_amount,
    failed_amount,
    money_breakdown,
    outstanding_amount,
    recovered_amount,
    refunded_amount,
    resolve_currency,
)
from app.services.tracing.reconstruction import reconstruct_order
from app.services.tracing.state import AttemptRecord
from tests.engine.conftest import order_timeline, subscription_timeline
from tests.tracing.conftest import ORDER_ID, event, scenario_result

MONEY_SCENARIOS = ("S01", "S02", "S03", "S04", "S04b", "S05", "S10", "S11", "S12", "S13")


class TestNoAttemptSumInflation:
    """One order, three tries at collecting it, one amount at risk."""

    def test_three_failures_risk_one_order_amount(self) -> None:
        timeline = order_timeline("S04")
        assert len(timeline.failed_attempts) == 3
        assert amount_at_risk_repeated_failure(timeline) == timeline.amount_minor

    def test_risk_is_not_the_sum_of_attempts(self) -> None:
        timeline = order_timeline("S04")
        attempt_sum = sum(a.amount_minor for a in timeline.attempts)

        assert attempt_sum == timeline.amount_minor * 3
        assert amount_at_risk_repeated_failure(timeline) != attempt_sum

    def test_risk_matches_the_generated_order_amount(self) -> None:
        result = scenario_result("S04")
        assert amount_at_risk_repeated_failure(order_timeline("S04")) == (
            result.entities.orders[0].amount
        )

    def test_two_failures_risk_the_same_amount_as_three(self) -> None:
        """Attempt count changes confidence, never the amount owed."""
        assert amount_at_risk_repeated_failure(order_timeline("S12")) == (
            order_timeline("S12").amount_minor
        )

    def test_failed_amount_is_also_order_level(self) -> None:
        timeline = order_timeline("S04")
        assert failed_amount(timeline) == timeline.amount_minor


class TestCapturedAmount:
    def test_zero_when_nothing_captured(self) -> None:
        assert captured_amount(order_timeline("S04")) == 0

    def test_equals_order_amount_when_paid(self) -> None:
        timeline = order_timeline("S01")
        assert captured_amount(timeline) == timeline.amount_minor

    def test_counts_one_success_once_after_failures(self) -> None:
        timeline = order_timeline("S03")
        assert captured_amount(timeline) == timeline.amount_minor

    def test_duplicate_delivery_does_not_double_count(self) -> None:
        """S07 redelivers the capture; the money still arrived once."""
        duplicated = order_timeline("S07")
        clean = order_timeline("S01")
        assert captured_amount(duplicated) == captured_amount(clean)

    def test_captured_even_when_order_never_reconciled(self) -> None:
        timeline = order_timeline("S10")
        assert captured_amount(timeline) == timeline.amount_minor


class TestFailedAmount:
    def test_zero_once_collected(self) -> None:
        assert failed_amount(order_timeline("S01")) == 0

    def test_zero_after_a_successful_retry(self) -> None:
        """S02 failed once then succeeded. Nothing was ultimately lost."""
        assert failed_amount(order_timeline("S02")) == 0

    def test_full_amount_when_never_collected(self) -> None:
        timeline = order_timeline("S04")
        assert failed_amount(timeline) == timeline.amount_minor

    def test_abandonment_counts_as_failed(self) -> None:
        timeline = order_timeline("S05")
        assert failed_amount(timeline) == timeline.amount_minor


class TestRefundedAmount:
    def test_zero_without_a_refund(self) -> None:
        assert refunded_amount(order_timeline("S01")) == 0

    def test_matches_the_refund_event(self) -> None:
        timeline = order_timeline("S13")
        assert refunded_amount(timeline) == timeline.amount_minor

    def test_is_clamped_at_the_captured_amount(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event(
                    "payment.attempted", 30, payment_ref="p1", attempt_number=1, amount_minor=1000
                ),
                event(
                    "payment.captured", 40, payment_ref="p1", attempt_number=1, amount_minor=1000
                ),
                event("refund.created", 90, refund_ref="r1", amount_minor=99_999),
            ],
        )
        assert refunded_amount(timeline) == 1000

    def test_float_refund_amount_is_ignored(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event(
                    "payment.attempted", 30, payment_ref="p1", attempt_number=1, amount_minor=1000
                ),
                event(
                    "payment.captured", 40, payment_ref="p1", attempt_number=1, amount_minor=1000
                ),
                event("refund.created", 90, refund_ref="r1", amount_minor=500.5),
            ],
        )
        assert refunded_amount(timeline) == 0


class TestRecoveredAmount:
    def test_zero_without_a_recovery(self) -> None:
        assert recovered_amount(order_timeline("S04")) == 0

    def test_recovery_success_recognises_the_capture(self) -> None:
        timeline = order_timeline("S11")
        assert recovered_amount(timeline) == timeline.amount_minor

    def test_recovery_failure_recovers_nothing(self) -> None:
        """Expected recovery must never be reported as actual recovery."""
        timeline = order_timeline("S12")
        assert timeline.has_recovery_action
        assert recovered_amount(timeline) == 0

    def test_a_recovery_event_alone_does_not_move_money(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event("order.created", 0, order_ref="o1", amount_minor=5000),
                event("recovery.action_executed", 100, action_ref="a1"),
                event("recovery.succeeded", 200, action_ref="a1"),
            ],
        )
        assert timeline.has_recovery_succeeded
        assert recovered_amount(timeline) == 0


class TestOutstandingAmount:
    def test_zero_when_paid_and_kept(self) -> None:
        assert outstanding_amount(order_timeline("S01")) == 0

    def test_full_amount_when_never_paid(self) -> None:
        timeline = order_timeline("S04")
        assert outstanding_amount(timeline) == timeline.amount_minor

    def test_refund_restores_the_outstanding_balance(self) -> None:
        timeline = order_timeline("S13")
        assert outstanding_amount(timeline) == timeline.amount_minor

    def test_never_negative(self) -> None:
        for scenario in MONEY_SCENARIOS:
            assert outstanding_amount(order_timeline(scenario)) >= 0


class TestReconciliationAmount:
    def test_is_always_zero(self) -> None:
        """Captured money is not revenue at risk — this is an integrity anomaly."""
        assert amount_at_risk_reconciliation(order_timeline("S10")) == 0

    def test_captured_money_is_still_visible(self) -> None:
        timeline = order_timeline("S10")
        assert amount_at_risk_reconciliation(timeline) == 0
        assert captured_amount(timeline) > 0


class TestAbandonmentAndSubscriptionAmounts:
    def test_abandonment_risks_the_order_amount(self) -> None:
        timeline = order_timeline("S05")
        assert amount_at_risk_checkout_abandonment(timeline) == timeline.amount_minor

    def test_subscription_sums_failed_cycles(self) -> None:
        """Here a sum IS right: each failed cycle is a separate lost charge."""
        subscription = subscription_timeline("S06")
        assert subscription.failed_cycles == 2
        assert amount_at_risk_subscription(subscription) == subscription.failed_amount_minor
        assert amount_at_risk_subscription(subscription) > 0

    def test_collected_order_risks_nothing(self) -> None:
        assert amount_at_risk_repeated_failure(order_timeline("S01")) == 0
        assert amount_at_risk_checkout_abandonment(order_timeline("S01")) == 0


class TestIntegerDiscipline:
    @pytest.mark.parametrize("scenario", MONEY_SCENARIOS)
    def test_every_figure_is_an_integer(self, scenario: str) -> None:
        breakdown = money_breakdown(order_timeline(scenario))
        for name in ("order_amount", "captured", "failed", "refunded", "recovered", "outstanding"):
            value = getattr(breakdown, name)
            assert isinstance(value, int), f"{scenario}.{name} is {type(value).__name__}"
            assert not isinstance(value, bool)

    @pytest.mark.parametrize("scenario", MONEY_SCENARIOS)
    def test_no_figure_is_negative(self, scenario: str) -> None:
        breakdown = money_breakdown(order_timeline(scenario))
        for name in ("order_amount", "captured", "failed", "refunded", "recovered", "outstanding"):
            assert getattr(breakdown, name) >= 0

    def test_breakdown_rejects_a_float(self) -> None:
        with pytest.raises(TypeError, match="integer count of minor units"):
            MoneyBreakdown(
                currency="INR",
                order_amount=100.5,  # type: ignore[arg-type]
                captured=0,
                failed=0,
                refunded=0,
                recovered=0,
                outstanding=0,
            )

    def test_breakdown_rejects_a_bool(self) -> None:
        with pytest.raises(TypeError):
            MoneyBreakdown(
                currency="INR",
                order_amount=True,  # type: ignore[arg-type]
                captured=0,
                failed=0,
                refunded=0,
                recovered=0,
                outstanding=0,
            )


class TestCurrencyConsistency:
    @pytest.mark.parametrize("scenario", MONEY_SCENARIOS)
    def test_currency_resolves(self, scenario: str) -> None:
        assert resolve_currency(order_timeline(scenario)) == "INR"

    def test_mismatched_attempt_currency_raises(self) -> None:
        """Combining currencies is a wrong answer, not a rounding problem."""
        timeline = order_timeline("S04")
        poisoned = timeline.attempts[0]
        mismatched = AttemptRecord(
            payment_ref=poisoned.payment_ref,
            attempt_number=poisoned.attempt_number,
            outcome=poisoned.outcome,
            payment_method=poisoned.payment_method,
            failure_code=poisoned.failure_code,
            failure_reason=poisoned.failure_reason,
            amount_minor=poisoned.amount_minor,
            currency="USD",
            first_seen_at=poisoned.first_seen_at,
            last_seen_at=poisoned.last_seen_at,
        )
        broken = type(timeline)(
            **{
                **{f: getattr(timeline, f) for f in timeline.__slots__},
                "attempts": (mismatched, *timeline.attempts[1:]),
            }
        )

        with pytest.raises(CurrencyMismatchError):
            resolve_currency(broken)

    def test_missing_attempt_currency_is_tolerated(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [event("order.created", 0, order_ref="o1", amount_minor=1000, currency="INR")],
        )
        assert resolve_currency(timeline) == "INR"


class TestDeterminism:
    @pytest.mark.parametrize("scenario", MONEY_SCENARIOS)
    def test_repeated_computation_is_identical(self, scenario: str) -> None:
        timeline = order_timeline(scenario)
        assert money_breakdown(timeline) == money_breakdown(timeline)

    def test_stable_across_rebuilt_timelines(self) -> None:
        assert money_breakdown(order_timeline("S04")) == money_breakdown(order_timeline("S04"))

    def test_delivery_pathology_does_not_change_the_money(self) -> None:
        """S08/S09/S12b must agree with S04 on every figure."""
        baseline = money_breakdown(order_timeline("S04"))
        for scenario in ("S08", "S09", "S12b"):
            assert money_breakdown(order_timeline(scenario)) == baseline
