"""The attempt ledger and gap inference.

Gap inference is what lets the missing-event scenario still reach the right
conclusion: an attempt whose `payment.attempted` event was never delivered is
recorded anyway, flagged, and counted — rather than silently looking like an
attempt that never happened.
"""

from __future__ import annotations

import pytest

from app.services.tracing.reconstruction import reconstruct_order
from app.services.tracing.state import AttemptOutcome
from tests.tracing.conftest import ORDER_ID, event, only_order, scenario_result


class TestLedgerConstruction:
    def test_one_record_per_payment_reference(self) -> None:
        timeline = only_order("S04")
        refs = [a.payment_ref for a in timeline.attempts]
        assert len(refs) == len(set(refs)) == 3

    def test_attempts_are_ordered_by_attempt_number(self) -> None:
        numbers = [a.attempt_number for a in only_order("S03").attempts]
        assert numbers == sorted(numbers) == [1, 2, 3]

    def test_ledger_matches_the_generated_attempts(self) -> None:
        result = scenario_result("S03")
        assert len(only_order("S03").attempts) == len(result.entities.payment_attempts)

    def test_abandonment_has_no_attempts(self) -> None:
        assert only_order("S05").attempts == ()

    def test_events_without_a_payment_ref_are_ignored(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event("order.created", 0, order_ref="o1"),
                event("payment.attempted", 30, payment_ref="p1", attempt_number=1),
            ],
        )
        assert len(timeline.attempts) == 1


class TestOutcomeDerivation:
    def test_capture_supersedes_attempt(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event("payment.attempted", 30, payment_ref="p1", attempt_number=1),
                event("payment.authorized", 34, payment_ref="p1", attempt_number=1),
                event("payment.captured", 40, payment_ref="p1", attempt_number=1),
            ],
        )
        assert timeline.attempts[0].outcome is AttemptOutcome.CAPTURED

    def test_failure_supersedes_attempt(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event("payment.attempted", 30, payment_ref="p1", attempt_number=1),
                event(
                    "payment.failed",
                    33,
                    payment_ref="p1",
                    attempt_number=1,
                    failure_code="card_declined",
                ),
            ],
        )
        assert timeline.attempts[0].outcome is AttemptOutcome.FAILED

    def test_outcome_is_independent_of_delivery_order(self) -> None:
        events = [
            event("payment.captured", 40, payment_ref="p1", attempt_number=1),
            event("payment.attempted", 30, payment_ref="p1", attempt_number=1),
        ]
        assert reconstruct_order(ORDER_ID, events).attempts[0].outcome is AttemptOutcome.CAPTURED

    def test_gateway_timeout_is_distinguished_from_a_decline(self) -> None:
        """A timeout warrants a different diagnosis from a hard decline."""
        timeline = only_order("S04c")
        assert all(a.outcome is AttemptOutcome.TIMEOUT for a in timeline.attempts)

    def test_hard_decline_is_not_a_timeout(self) -> None:
        assert all(a.outcome is AttemptOutcome.FAILED for a in only_order("S04").attempts)

    def test_refund_is_an_order_level_fact_for_simulated_data(self) -> None:
        """The Phase 2 refund payload names no payment_ref.

        Attributing the refund to a specific attempt would be a guess, so the
        attempt stays `captured` and the refund shows at order level — which is
        all detection needs to know a refund is not a leak.
        """
        timeline = only_order("S13")
        assert timeline.attempts[0].outcome is AttemptOutcome.CAPTURED
        assert timeline.has_refund
        assert timeline.state == "refunded"

    def test_refund_supersedes_capture_when_a_payment_ref_is_given(self) -> None:
        """A provider that names the payment does get attempt-level attribution."""
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event("payment.attempted", 30, payment_ref="p1", attempt_number=1),
                event("payment.captured", 40, payment_ref="p1", attempt_number=1),
                event("refund.created", 86_400, payment_ref="p1", amount_minor=1000),
            ],
        )
        assert timeline.attempts[0].outcome is AttemptOutcome.REFUNDED

    def test_timeout_counts_as_a_failure(self) -> None:
        assert len(only_order("S04c").failed_attempts) == 2

    def test_refunded_counts_as_successful(self) -> None:
        """The money did arrive; a refund is a later business decision."""
        assert only_order("S13").successful_attempts


class TestFailureDetails:
    def test_failure_code_is_captured(self) -> None:
        assert all(a.failure_code == "card_declined" for a in only_order("S04").attempts)

    def test_failure_reason_is_captured(self) -> None:
        assert all(a.failure_reason for a in only_order("S04").attempts)

    def test_successful_attempts_have_no_failure_code(self) -> None:
        assert only_order("S01").attempts[0].failure_code is None

    def test_payment_method_is_captured(self) -> None:
        assert only_order("S04c").attempts[0].payment_method == "upi"


class TestGapInference:
    """S12b — one payment.attempted event is never delivered."""

    def test_missing_attempted_event_still_yields_an_attempt(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event(
                    "payment.failed",
                    33,
                    payment_ref="p1",
                    attempt_number=1,
                    failure_code="card_declined",
                )
            ],
        )
        assert len(timeline.attempts) == 1

    def test_the_inferred_attempt_is_flagged(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event(
                    "payment.failed",
                    33,
                    payment_ref="p1",
                    attempt_number=1,
                    failure_code="card_declined",
                )
            ],
        )
        assert timeline.attempts[0].inferred is True

    def test_inference_is_counted_in_integrity(self) -> None:
        assert only_order("S12b").integrity.inferred_gaps == 1

    def test_a_complete_attempt_is_not_flagged(self) -> None:
        assert all(not a.inferred for a in only_order("S04").attempts)

    def test_missing_event_preserves_the_conclusion(self) -> None:
        """The whole point: detection must still see three failed attempts."""
        with_gap = only_order("S12b")
        complete = only_order("S04")

        assert len(with_gap.attempts) == len(complete.attempts)
        assert len(with_gap.failed_attempts) == len(complete.failed_attempts)
        assert with_gap.reached_terminal_success == complete.reached_terminal_success

    def test_exactly_one_attempt_is_inferred_in_s12b(self) -> None:
        assert sum(1 for a in only_order("S12b").attempts if a.inferred) == 1


class TestLedgerMoney:
    def test_amounts_are_integers(self) -> None:
        for attempt in only_order("S04").attempts:
            assert isinstance(attempt.amount_minor, int)
            assert not isinstance(attempt.amount_minor, bool)

    def test_float_amount_is_not_coerced(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [event("payment.attempted", 30, payment_ref="p1", attempt_number=1, amount_minor=99.5)],
        )
        assert timeline.attempts[0].amount_minor == 0

    def test_captured_amount_sums_only_successes(self) -> None:
        """Three failed attempts on one order must not sum to three orders."""
        timeline = only_order("S04")
        assert timeline.captured_amount_minor == 0
        assert timeline.amount_minor > 0

    def test_captured_amount_counts_one_success_once(self) -> None:
        timeline = only_order("S03")
        assert timeline.captured_amount_minor == timeline.amount_minor

    @pytest.mark.parametrize("scenario", ["S01", "S02", "S03", "S04", "S12b", "S13"])
    def test_currency_is_recorded(self, scenario: str) -> None:
        for attempt in only_order(scenario).attempts:
            assert attempt.currency == "INR"


class TestSubscriptionLedger:
    def test_cycles_are_counted(self) -> None:
        from app.services.tracing.reconstruction import reconstruct_merchant
        from tests.tracing.conftest import merchant_id_of, scenario_events

        timeline = reconstruct_merchant(merchant_id_of("S06"), scenario_events("S06"))
        subscription = timeline.subscriptions[0]

        assert subscription.charged_cycles == 2
        assert subscription.failed_cycles == 2
        assert subscription.is_halted

    def test_trailing_failure_streak(self) -> None:
        from app.services.tracing.reconstruction import reconstruct_merchant
        from tests.tracing.conftest import merchant_id_of, scenario_events

        timeline = reconstruct_merchant(merchant_id_of("S06"), scenario_events("S06"))
        assert timeline.subscriptions[0].trailing_failure_streak == 2

    def test_failed_amount_is_an_integer_sum(self) -> None:
        from app.services.tracing.reconstruction import reconstruct_merchant
        from tests.tracing.conftest import merchant_id_of, scenario_events

        timeline = reconstruct_merchant(merchant_id_of("S06"), scenario_events("S06"))
        subscription = timeline.subscriptions[0]

        assert isinstance(subscription.failed_amount_minor, int)
        assert subscription.failed_amount_minor > 0
