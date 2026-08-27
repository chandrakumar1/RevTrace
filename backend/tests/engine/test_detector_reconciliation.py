"""Reconciliation mismatch detector, with its negative controls.

Two properties carry the design:

* `amount_at_risk` is always **0** — the money arrived, so counting it as at
  risk would inflate every total with collected funds.
* A delayed `order.paid` must not be mistaken for a missing one. The grace
  period is what separates late from absent.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.engine.detectors.config import DetectorConfig
from app.engine.detectors.reconciliation import DETECTION_RULE, detect
from app.engine.risk_engine import captured_amount
from app.models.enums import RiskType
from app.services.tracing.reconstruction import reconstruct_order
from tests.engine.conftest import AS_OF, order_timeline
from tests.tracing.conftest import EPOCH, ORDER_ID, event

NEGATIVE = (
    "S01",
    "S02",
    "S03",
    "S04",
    "S04b",
    "S04c",
    "S05",
    "S07",
    "S08",
    "S09",
    "S11",
    "S12",
    "S12b",
    "S13",
)


def _captured_not_paid():
    return reconstruct_order(
        ORDER_ID,
        [
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
                "payment.captured",
                40,
                payment_ref="p1",
                attempt_number=1,
                amount_minor=5000,
                currency="INR",
            ),
        ],
    )


class TestPositiveDetection:
    def test_fires_on_captured_but_unreconciled(self) -> None:
        assert len(detect(order_timeline("S10"), AS_OF)) == 1

    def test_reports_the_right_risk_type(self) -> None:
        finding = detect(order_timeline("S10"), AS_OF)[0]
        assert finding.risk_type == RiskType.RECONCILIATION_MISMATCH.value

    def test_records_the_rule_version(self) -> None:
        assert detect(order_timeline("S10"), AS_OF)[0].detection_rule == DETECTION_RULE

    def test_evidence_points_at_the_capture(self) -> None:
        timeline = order_timeline("S10")
        finding = detect(timeline, AS_OF)[0]
        known = {e.external_event_id for e in timeline.events}

        assert finding.evidence_event_ids
        assert set(finding.evidence_event_ids) <= known

    def test_reason_explains_no_revenue_is_at_risk(self) -> None:
        reason = detect(order_timeline("S10"), AS_OF)[0].reason
        assert "No revenue is at risk" in reason
        assert "order.paid never arrived" in reason

    def test_reason_reports_the_captured_amount(self) -> None:
        timeline = order_timeline("S10")
        assert str(captured_amount(timeline)) in detect(timeline, AS_OF)[0].reason

    def test_detected_at_is_the_supplied_instant(self) -> None:
        assert detect(order_timeline("S10"), AS_OF)[0].detected_at == AS_OF


class TestAmountIsAlwaysZero:
    """The money arrived. This is an integrity anomaly, not lost revenue."""

    def test_amount_at_risk_is_zero(self) -> None:
        assert detect(order_timeline("S10"), AS_OF)[0].amount_at_risk == 0

    def test_zero_even_though_the_order_has_value(self) -> None:
        timeline = order_timeline("S10")
        assert timeline.amount_minor > 0
        assert detect(timeline, AS_OF)[0].amount_at_risk == 0

    def test_captured_money_remains_visible(self) -> None:
        timeline = order_timeline("S10")
        assert captured_amount(timeline) > 0
        assert detect(timeline, AS_OF)[0].amount_at_risk == 0

    def test_does_not_contribute_to_an_at_risk_total(self) -> None:
        findings = detect(order_timeline("S10"), AS_OF)
        assert sum(f.amount_at_risk for f in findings) == 0


class TestNegativeControls:
    @pytest.mark.parametrize("scenario", NEGATIVE)
    def test_produces_nothing(self, scenario: str) -> None:
        assert detect(order_timeline(scenario), AS_OF) == ()

    def test_a_reconciled_order_is_silent(self) -> None:
        timeline = order_timeline("S01")
        assert timeline.has_capture and timeline.has_order_paid
        assert detect(timeline, AS_OF) == ()

    def test_an_uncaptured_order_is_silent(self) -> None:
        timeline = order_timeline("S04")
        assert not timeline.has_capture
        assert detect(timeline, AS_OF) == ()

    def test_a_refund_is_not_a_mismatch(self) -> None:
        """A deliberate reversal, not a bookkeeping failure."""
        timeline = order_timeline("S13")
        assert timeline.has_capture and timeline.has_refund
        assert detect(timeline, AS_OF) == ()

    def test_recovered_order_is_silent(self) -> None:
        assert detect(order_timeline("S11"), AS_OF) == ()


class TestGracePeriod:
    """A delayed order.paid must not be mistaken for a missing one."""

    def test_silent_within_the_grace_period(self) -> None:
        timeline = _captured_not_paid()
        assert detect(timeline, EPOCH + timedelta(minutes=5)) == ()

    def test_fires_after_the_grace_period(self) -> None:
        timeline = _captured_not_paid()
        assert len(detect(timeline, EPOCH + timedelta(hours=2))) == 1

    def test_boundary_is_respected_exactly(self) -> None:
        timeline = _captured_not_paid()
        config = DetectorConfig(reconciliation_grace_seconds=3_600)
        capture_at = EPOCH + timedelta(seconds=40)

        assert detect(timeline, capture_at + timedelta(seconds=3_599), config) == ()
        assert len(detect(timeline, capture_at + timedelta(seconds=3_600), config)) == 1

    def test_reason_reports_how_long_it_waited(self) -> None:
        timeline = _captured_not_paid()
        finding = detect(timeline, EPOCH + timedelta(hours=2))[0]
        assert "seconds" in finding.reason

    def test_a_late_order_paid_suppresses_the_finding(self) -> None:
        """The event was late, not missing. Detection must retract."""
        late = reconstruct_order(
            ORDER_ID,
            [
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
                    "payment.captured",
                    40,
                    payment_ref="p1",
                    attempt_number=1,
                    amount_minor=5000,
                    currency="INR",
                ),
                event("order.paid", 50, order_ref="o1", amount_minor=5000, currency="INR"),
            ],
        )
        assert detect(late, EPOCH + timedelta(hours=2)) == ()


class TestDeterminism:
    def test_repeated_runs_are_identical(self) -> None:
        timeline = order_timeline("S10")
        assert detect(timeline, AS_OF) == detect(timeline, AS_OF)

    def test_natural_key_is_stable(self) -> None:
        finding = detect(order_timeline("S10"), AS_OF)[0]
        assert finding.natural_key == (
            finding.merchant_id,
            finding.order_id,
            RiskType.RECONCILIATION_MISMATCH.value,
        )
