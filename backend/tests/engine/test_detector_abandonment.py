"""Checkout abandonment detector, with its negative controls.

The trap this detector must avoid: treating "no payment attempt yet" as
"abandoned". A checkout that started a minute ago is in progress, not lost.
"""

from __future__ import annotations

import pytest

from app.engine.detectors.abandonment import DETECTION_RULE, detect
from app.engine.detectors.config import DetectorConfig
from app.models.enums import RiskType
from app.services.tracing.reconstruction import reconstruct_order
from tests.engine.conftest import AS_OF, AS_OF_IMMEDIATE, order_timeline
from tests.tracing.conftest import ORDER_ID, event

#: Every scenario that involves a payment attempt or a completed sale.
NEGATIVE = (
    "S01",
    "S02",
    "S03",
    "S04",
    "S04b",
    "S04c",
    "S07",
    "S08",
    "S09",
    "S10",
    "S11",
    "S12",
    "S12b",
    "S13",
)


class TestPositiveDetection:
    def test_fires_on_abandonment(self) -> None:
        assert len(detect(order_timeline("S05"), AS_OF)) == 1

    def test_reports_the_right_risk_type(self) -> None:
        finding = detect(order_timeline("S05"), AS_OF)[0]
        assert finding.risk_type == RiskType.CHECKOUT_ABANDONMENT.value

    def test_amount_is_the_order_amount(self) -> None:
        timeline = order_timeline("S05")
        assert detect(timeline, AS_OF)[0].amount_at_risk == timeline.amount_minor

    def test_records_the_rule_version(self) -> None:
        assert detect(order_timeline("S05"), AS_OF)[0].detection_rule == DETECTION_RULE

    def test_evidence_covers_the_checkout_events(self) -> None:
        timeline = order_timeline("S05")
        finding = detect(timeline, AS_OF)[0]
        known = {e.external_event_id for e in timeline.events}

        assert len(finding.evidence_event_ids) >= 2
        assert set(finding.evidence_event_ids) <= known

    def test_explicit_abandonment_is_named_in_the_reason(self) -> None:
        assert "abandoned" in detect(order_timeline("S05"), AS_OF)[0].reason

    def test_detected_at_is_the_supplied_instant(self) -> None:
        assert detect(order_timeline("S05"), AS_OF)[0].detected_at == AS_OF


class TestNegativeControls:
    @pytest.mark.parametrize("scenario", NEGATIVE)
    def test_produces_nothing(self, scenario: str) -> None:
        assert detect(order_timeline(scenario), AS_OF) == ()

    def test_a_payment_attempt_rules_out_abandonment(self) -> None:
        """S04 has attempts — it is a payment failure, not an abandonment."""
        timeline = order_timeline("S04")
        assert timeline.attempts
        assert detect(timeline, AS_OF) == ()

    def test_a_completed_sale_is_not_abandonment(self) -> None:
        assert detect(order_timeline("S01"), AS_OF) == ()

    def test_refunded_order_is_not_abandonment(self) -> None:
        assert detect(order_timeline("S13"), AS_OF) == ()

    def test_a_checkout_still_in_progress_is_not_abandoned(self) -> None:
        """The most important guard: silence is only meaningful over time."""
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event("checkout.started", 0, session_ref="s1", amount_minor=5000, currency="INR"),
                event("order.created", 5, order_ref="o1", amount_minor=5000, currency="INR"),
            ],
        )
        assert detect(timeline, AS_OF_IMMEDIATE) == ()

    def test_the_same_checkout_is_abandoned_once_silence_elapses(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [
                event("checkout.started", 0, session_ref="s1", amount_minor=5000, currency="INR"),
                event("order.created", 5, order_ref="o1", amount_minor=5000, currency="INR"),
            ],
        )
        assert len(detect(timeline, AS_OF)) == 1

    def test_silence_threshold_is_respected_exactly(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [event("checkout.started", 0, session_ref="s1", amount_minor=5000, currency="INR")],
        )
        config = DetectorConfig(abandonment_silence_seconds=1_800)

        from datetime import timedelta

        from tests.tracing.conftest import EPOCH

        assert detect(timeline, EPOCH + timedelta(seconds=1_799), config) == ()
        assert len(detect(timeline, EPOCH + timedelta(seconds=1_800), config)) == 1


class TestInferredVersusExplicit:
    """An explicit abandonment event is stronger evidence than silence."""

    def test_explicit_scores_higher_than_inferred(self) -> None:
        explicit = detect(order_timeline("S05"), AS_OF)[0]

        inferred_timeline = reconstruct_order(
            ORDER_ID,
            [
                event("checkout.started", 0, session_ref="s1", amount_minor=5000, currency="INR"),
                event("order.created", 5, order_ref="o1", amount_minor=5000, currency="INR"),
            ],
        )
        inferred = detect(inferred_timeline, AS_OF)[0]

        assert explicit.confidence_bps > inferred.confidence_bps

    def test_inferred_reason_reports_the_silence(self) -> None:
        timeline = reconstruct_order(
            ORDER_ID,
            [event("checkout.started", 0, session_ref="s1", amount_minor=5000, currency="INR")],
        )
        assert "no activity for" in detect(timeline, AS_OF)[0].reason


class TestDeterminism:
    def test_repeated_runs_are_identical(self) -> None:
        timeline = order_timeline("S05")
        assert detect(timeline, AS_OF) == detect(timeline, AS_OF)

    def test_as_of_is_the_only_time_input(self) -> None:
        """Two different instants may differ; the same instant never does."""
        timeline = order_timeline("S05")
        assert detect(timeline, AS_OF)[0] == detect(timeline, AS_OF)[0]
