"""Delivery pathology: duplicates, delays, reordering, drops.

The central assertion across this file: pathological delivery must not change
what the underlying history actually says.
"""

from __future__ import annotations

import pytest
from simulator import simulate
from simulator.config import LONG_DELIVERY_DELAY_SECONDS, ScenarioParams
from simulator.delivery import DeliveryPlan, apply_delivery_plan

from .conftest import SEED


class TestDuplicateDelivery:
    def test_emits_more_deliveries_than_unique_events(self) -> None:
        result = simulate("S07", seed=SEED)
        ids = [d.event.external_event_id for d in result.deliveries]
        assert len(ids) > len(set(ids))

    def test_duplicates_share_one_external_event_id(self) -> None:
        result = simulate("S07", seed=SEED)
        duplicates = [d for d in result.deliveries if d.envelope.is_duplicate]
        assert duplicates
        for duplicate in duplicates:
            originals = [
                d
                for d in result.deliveries
                if d.event.external_event_id == duplicate.event.external_event_id
                and not d.envelope.is_duplicate
            ]
            assert len(originals) == 1

    def test_duplicate_differs_only_in_received_at(self) -> None:
        result = simulate("S07", seed=SEED)
        by_id: dict[str, list] = {}
        for delivery in result.deliveries:
            by_id.setdefault(delivery.event.external_event_id, []).append(delivery)

        for deliveries in by_id.values():
            if len(deliveries) < 2:
                continue
            first, *rest = deliveries
            for other in rest:
                assert other.event.occurred_at == first.event.occurred_at
                assert other.event.event_type == first.event.event_type
                assert other.event.payload == first.event.payload
                assert other.event.received_at > first.event.received_at

    def test_delivery_attempt_increments(self) -> None:
        result = simulate("S07", seed=SEED)
        for delivery in result.deliveries:
            if delivery.envelope.is_duplicate:
                assert delivery.envelope.delivery_attempt >= 2
            else:
                assert delivery.envelope.delivery_attempt == 1

    def test_ground_truth_records_the_suppression_expectation(self) -> None:
        truth = simulate("S07", seed=SEED).ground_truth
        assert truth.expected_persisted_event_count < truth.emitted_event_count
        assert truth.duplicated_events

    def test_simulator_never_deduplicates(self) -> None:
        """Suppression is the database's job, not the simulator's."""
        result = simulate("S07", seed=SEED)
        assert len(result.deliveries) == result.ground_truth.emitted_event_count

    def test_duplicate_count_is_configurable(self) -> None:
        more = simulate("S07", seed=SEED, params=ScenarioParams(duplicate_count=3))
        default = simulate("S07", seed=SEED)
        assert len(more.deliveries) > len(default.deliveries)

    def test_detection_outcome_matches_clean_counterpart(self) -> None:
        """S07 is S01 with duplicates. The conclusion must be identical."""
        assert (
            simulate("S07", seed=SEED).ground_truth.expected_risks
            == simulate("S01", seed=SEED).ground_truth.expected_risks
        )


class TestOutOfOrderDelivery:
    def test_arrival_order_differs_from_causal_order(self) -> None:
        result = simulate("S08", seed=SEED)
        arrival = [d.event.occurred_at for d in result.deliveries]
        assert arrival != sorted(arrival)

    def test_flagged_as_out_of_order(self) -> None:
        result = simulate("S08", seed=SEED)
        assert any(d.envelope.is_out_of_order for d in result.deliveries)

    def test_causal_order_still_reconstructs(self) -> None:
        events = simulate("S08", seed=SEED).events_in_causal_order
        times = [e.occurred_at for e in events]
        assert times == sorted(times)

    def test_same_events_as_clean_counterpart(self) -> None:
        """Reordering changes arrival, never content."""
        scrambled = simulate("S08", seed=SEED)
        clean = simulate("S04", seed=SEED)
        assert [e.event_type for e in scrambled.events_in_causal_order] == [
            e.event_type for e in clean.events_in_causal_order
        ]

    def test_detection_outcome_matches_clean_counterpart(self) -> None:
        assert (
            simulate("S08", seed=SEED).ground_truth.expected_risks
            == simulate("S04", seed=SEED).ground_truth.expected_risks
        )


class TestDelayedDelivery:
    def test_a_delivery_is_flagged_delayed(self) -> None:
        result = simulate("S09", seed=SEED)
        assert any(d.envelope.is_delayed for d in result.deliveries)

    def test_delay_gap_is_applied(self) -> None:
        result = simulate("S09", seed=SEED)
        delayed = [d for d in result.deliveries if d.envelope.is_delayed]
        assert delayed
        for delivery in delayed:
            gap = (delivery.event.received_at - delivery.event.occurred_at).total_seconds()
            assert gap >= LONG_DELIVERY_DELAY_SECONDS

    def test_occurred_at_is_untouched_by_delay(self) -> None:
        delayed = simulate("S09", seed=SEED)
        clean = simulate("S04", seed=SEED)
        assert [e.occurred_at for e in delayed.events_in_causal_order] == [
            e.occurred_at for e in clean.events_in_causal_order
        ]

    def test_delay_is_configurable(self) -> None:
        result = simulate("S09", seed=SEED, params=ScenarioParams(delay_seconds=7_200))
        delayed = [d for d in result.deliveries if d.envelope.is_delayed]
        assert delayed
        assert any(d.envelope.delay_seconds == 7_200 for d in delayed)

    def test_detection_outcome_matches_clean_counterpart(self) -> None:
        assert (
            simulate("S09", seed=SEED).ground_truth.expected_risks
            == simulate("S04", seed=SEED).ground_truth.expected_risks
        )


class TestMissingEvents:
    def test_fewer_deliveries_than_the_clean_counterpart(self) -> None:
        assert len(simulate("S12b", seed=SEED).deliveries) < len(
            simulate("S04", seed=SEED).deliveries
        )

    def test_ground_truth_names_the_dropped_event(self) -> None:
        truth = simulate("S12b", seed=SEED).ground_truth
        assert len(truth.dropped_events) == 1

    def test_dropped_event_is_genuinely_absent(self) -> None:
        result = simulate("S12b", seed=SEED)
        dropped = set(result.ground_truth.dropped_events)
        delivered = {d.event.external_event_id for d in result.deliveries}
        assert not (dropped & delivered)

    def test_risk_is_still_detectable(self) -> None:
        """Detection must degrade gracefully, not go silent."""
        assert simulate("S12b", seed=SEED).ground_truth.expected_risks


class TestDeliveryPlanValidation:
    def test_rejects_out_of_range_duplicate_index(self) -> None:
        events = simulate("S01", seed=SEED).events_in_causal_order
        with pytest.raises(ValueError, match="outside range"):
            apply_delivery_plan(events, DeliveryPlan(duplicates={99: 1}))

    def test_rejects_out_of_range_drop(self) -> None:
        events = simulate("S01", seed=SEED).events_in_causal_order
        with pytest.raises(ValueError, match="outside range"):
            apply_delivery_plan(events, DeliveryPlan(drops=frozenset({99})))

    def test_rejects_negative_delay(self) -> None:
        events = simulate("S01", seed=SEED).events_in_causal_order
        with pytest.raises(ValueError, match="non-negative"):
            apply_delivery_plan(events, DeliveryPlan(delays={0: -5}))

    def test_rejects_incomplete_reorder(self) -> None:
        events = simulate("S01", seed=SEED).events_in_causal_order
        with pytest.raises(ValueError, match="permutation"):
            apply_delivery_plan(events, DeliveryPlan(reorder=(0, 1)))

    def test_empty_plan_preserves_everything(self) -> None:
        events = simulate("S01", seed=SEED).events_in_causal_order
        deliveries = apply_delivery_plan(events, DeliveryPlan())
        assert len(deliveries) == len(events)
        assert all(not d.envelope.is_duplicate for d in deliveries)


class TestSequenceNumbering:
    def test_sequences_are_contiguous_from_one(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        sequences = [d.envelope.sequence for d in result.deliveries]
        assert sequences == list(range(1, len(sequences) + 1))
