"""Generated events must satisfy the Phase 1 `events` contract exactly.

Schema conformance is checked by introspecting SQLAlchemy metadata — no
database connection is opened.
"""

from __future__ import annotations

import pytest
from simulator import simulate
from simulator.events import FORBIDDEN_PAYLOAD_KEYS

from app.models import Base
from app.models.enums import EventType, OrderStatus, PaymentMethod, PaymentStatus

from .conftest import SEED

EVENTS_TABLE = Base.metadata.tables["events"]


class TestEnumConformance:
    def test_event_types_are_phase_1_values(self, scenario_id: str) -> None:
        valid = set(EventType.values())
        for delivery in simulate(scenario_id, seed=SEED).deliveries:
            assert delivery.event.event_type in valid

    def test_order_statuses_are_phase_1_values(self, scenario_id: str) -> None:
        valid = set(OrderStatus.values())
        for order in simulate(scenario_id, seed=SEED).entities.orders:
            assert order.status in valid

    def test_payment_statuses_are_phase_1_values(self, scenario_id: str) -> None:
        valid = set(PaymentStatus.values())
        for attempt in simulate(scenario_id, seed=SEED).entities.payment_attempts:
            assert attempt.status in valid

    def test_payment_methods_are_phase_1_values(self, scenario_id: str) -> None:
        valid = set(PaymentMethod.values())
        for attempt in simulate(scenario_id, seed=SEED).entities.payment_attempts:
            assert attempt.payment_method in valid

    def test_no_chargeback_event_types(self, scenario_id: str) -> None:
        """Chargebacks were deferred; nothing should have crept in."""
        for delivery in simulate(scenario_id, seed=SEED).deliveries:
            assert "chargeback" not in delivery.event.event_type


class TestSchemaConformance:
    def test_external_event_id_fits_the_column(self, scenario_id: str) -> None:
        limit = EVENTS_TABLE.columns["external_event_id"].type.length
        for delivery in simulate(scenario_id, seed=SEED).deliveries:
            assert len(delivery.event.external_event_id) <= limit

    def test_event_type_fits_the_column(self, scenario_id: str) -> None:
        limit = EVENTS_TABLE.columns["event_type"].type.length
        for delivery in simulate(scenario_id, seed=SEED).deliveries:
            assert len(delivery.event.event_type) <= limit

    def test_merchant_id_always_present(self, scenario_id: str) -> None:
        """events.merchant_id is NOT NULL in the Phase 1 schema."""
        for delivery in simulate(scenario_id, seed=SEED).deliveries:
            assert delivery.event.merchant_id is not None

    def test_payload_is_a_dict(self, scenario_id: str) -> None:
        for delivery in simulate(scenario_id, seed=SEED).deliveries:
            assert isinstance(delivery.event.payload, dict)

    def test_payload_is_json_serializable(self, scenario_id: str) -> None:
        import json

        for delivery in simulate(scenario_id, seed=SEED).deliveries:
            json.dumps(delivery.event.payload)


class TestGroundTruthIsolation:
    """Detection must not be able to read the answer from its own input."""

    def test_no_forbidden_keys_in_payloads(self, scenario_id: str) -> None:
        for delivery in simulate(scenario_id, seed=SEED).deliveries:
            leaked = FORBIDDEN_PAYLOAD_KEYS & set(delivery.event.payload)
            assert not leaked, f"payload leaked ground truth: {sorted(leaked)}"

    def test_scenario_id_never_appears_in_a_payload(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        for delivery in result.deliveries:
            rendered = str(delivery.event.payload)
            assert result.manifest.scenario_name not in rendered

    def test_risk_types_never_appear_in_payloads(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        for risk in result.ground_truth.expected_risks:
            for delivery in result.deliveries:
                assert risk.risk_type not in str(delivery.event.payload)

    def test_narrative_never_appears_in_payloads(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        narrative = result.ground_truth.narrative
        if not narrative:
            return
        for delivery in result.deliveries:
            assert narrative not in str(delivery.event.payload)


class TestCausalOrdering:
    def test_authorized_never_precedes_attempted(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        events = result.events_in_causal_order
        first_attempt = next(
            (e.occurred_at for e in events if e.event_type == EventType.PAYMENT_ATTEMPTED.value),
            None,
        )
        for event in events:
            if event.event_type == EventType.PAYMENT_AUTHORIZED.value:
                assert first_attempt is not None and event.occurred_at >= first_attempt

    def test_captured_never_precedes_authorized(self, scenario_id: str) -> None:
        events = simulate(scenario_id, seed=SEED).events_in_causal_order
        authorized = [
            e.occurred_at for e in events if e.event_type == EventType.PAYMENT_AUTHORIZED.value
        ]
        captured = [
            e.occurred_at for e in events if e.event_type == EventType.PAYMENT_CAPTURED.value
        ]
        if authorized and captured:
            assert min(captured) >= min(authorized)

    def test_order_created_precedes_order_events(self, scenario_id: str) -> None:
        events = simulate(scenario_id, seed=SEED).events_in_causal_order
        created: dict[object, object] = {}
        for event in events:
            if event.event_type == EventType.ORDER_CREATED.value and event.order_id is not None:
                created.setdefault(event.order_id, event.occurred_at)

        for event in events:
            if event.order_id in created and event.event_type != EventType.CHECKOUT_STARTED.value:
                assert event.occurred_at >= created[event.order_id]

    def test_abandonment_follows_checkout_start(self) -> None:
        events = simulate("S05", seed=SEED).events_in_causal_order
        started = next(e for e in events if e.event_type == EventType.CHECKOUT_STARTED.value)
        abandoned = next(e for e in events if e.event_type == EventType.CHECKOUT_ABANDONED.value)
        assert abandoned.occurred_at > started.occurred_at

    def test_causal_order_is_sorted_by_occurred_at(self, scenario_id: str) -> None:
        events = simulate(scenario_id, seed=SEED).events_in_causal_order
        times = [e.occurred_at for e in events]
        assert times == sorted(times)

    def test_causal_view_deduplicates(self) -> None:
        """The reconstructed timeline holds unique events, even with redelivery."""
        result = simulate("S07", seed=SEED)
        assert len(result.events_in_causal_order) < len(result.deliveries)


class TestExternalEventIds:
    def test_format(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        prefix = f"sim_evt_{result.manifest.scenario_id}_{SEED}_"
        for delivery in result.deliveries:
            assert delivery.event.external_event_id.startswith(prefix)

    def test_unique_except_deliberate_duplicates(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        ids = [d.event.external_event_id for d in result.deliveries]
        duplicated = set(result.ground_truth.duplicated_events)
        non_duplicate = [i for i in ids if i not in duplicated]
        assert len(non_duplicate) == len(set(non_duplicate))

    @pytest.mark.parametrize("seed", [0, 1, 42])
    def test_ids_incorporate_the_seed(self, seed: int) -> None:
        result = simulate("S04", seed=seed)
        assert f"_{seed}_" in result.deliveries[0].event.external_event_id
