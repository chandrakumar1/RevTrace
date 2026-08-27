"""Deduplication, ordering, and delivery-integrity measurement.

The property under test throughout: pathological delivery must not change what
the reconstructed timeline says.
"""

from __future__ import annotations

from app.models.enums import EventType
from app.services.tracing.reconstruction import (
    causal_order,
    deduplicate,
    delivery_order,
    reconstruct_merchant,
    reconstruct_order,
    timeline_view,
)
from tests.tracing.conftest import (
    ORDER_ID,
    event,
    merchant_id_of,
    only_order,
    reversed_delivery,
    scenario_events,
)


class TestDeduplication:
    def test_first_arrival_is_kept(self) -> None:
        first = event("order.created", 0, received=2, external_id="e1")
        repeat = event("order.created", 0, received=90, external_id="e1")

        unique, _ = deduplicate([first, repeat])
        assert len(unique) == 1
        assert unique[0].received_at == first.received_at

    def test_every_arrival_is_observed(self) -> None:
        first = event("order.created", 0, received=2, external_id="e1")
        repeat = event("order.created", 0, received=90, external_id="e1")

        _, observations = deduplicate([first, repeat])
        assert len(observations) == 2
        assert [o.suppressed for o in observations] == [False, True]

    def test_nothing_is_discarded(self) -> None:
        """Forensics must still be able to answer 'how many times did it arrive'."""
        deliveries = [event("order.created", 0, received=r, external_id="e1") for r in (1, 5, 9)]
        unique, observations = deduplicate(deliveries)

        assert len(unique) == 1
        assert len(observations) == 3
        assert sum(1 for o in observations if o.suppressed) == 2

    def test_distinct_events_are_all_kept(self) -> None:
        unique, observations = deduplicate(
            [event("order.created", 0), event("payment.attempted", 30)]
        )
        assert len(unique) == 2
        assert not any(o.suppressed for o in observations)

    def test_events_without_an_external_id_are_never_deduplicated(self) -> None:
        class NoId(type(event("order.created", 0))):  # type: ignore[misc]
            pass

        first = event("order.created", 0)
        second = event("order.created", 0)
        first.external_event_id = None  # type: ignore[assignment]
        second.external_event_id = None  # type: ignore[assignment]

        unique, observations = deduplicate([first, second])
        assert len(unique) == 2
        assert observations == ()

    def test_real_duplicate_scenario_is_deduplicated(self) -> None:
        events = scenario_events("S07")
        unique, observations = deduplicate(events)

        assert len(unique) < len(events)
        assert sum(1 for o in observations if o.suppressed) == len(events) - len(unique)


class TestCausalOrdering:
    def test_sorts_by_occurrence(self) -> None:
        ordered = causal_order([event("payment.failed", 90), event("order.created", 0)])
        assert [e.event_type for e in ordered] == ["order.created", "payment.failed"]

    def test_tiebreak_is_stable(self) -> None:
        """Equal occurrence times must still produce a total, repeatable order."""
        a = event("payment.attempted", 30, external_id="evt_b")
        b = event("payment.attempted", 30, external_id="evt_a")

        assert [e.external_event_id for e in causal_order([a, b])] == ["evt_a", "evt_b"]
        assert causal_order([a, b]) == causal_order([b, a])

    def test_delivery_order_is_separate_from_causal_order(self) -> None:
        late = event("order.created", 0, received=9000)
        prompt = event("payment.failed", 90, received=95)

        assert [e.event_type for e in causal_order([late, prompt])] == [
            "order.created",
            "payment.failed",
        ]
        assert [e.event_type for e in delivery_order([late, prompt])] == [
            "payment.failed",
            "order.created",
        ]

    def test_reversed_delivery_reconstructs_identically(self) -> None:
        events = scenario_events("S04")
        forward = reconstruct_order(ORDER_ID, events)
        backward = reconstruct_order(ORDER_ID, reversed_delivery(events))

        assert [e.external_event_id for e in forward.events] == [
            e.external_event_id for e in backward.events
        ]

    def test_shuffled_delivery_reconstructs_identically(self) -> None:
        events = scenario_events("S03")
        rotated = events[3:] + events[:3]

        assert [e.external_event_id for e in reconstruct_order(ORDER_ID, events).events] == [
            e.external_event_id for e in reconstruct_order(ORDER_ID, rotated).events
        ]


class TestDeliveryPathologyDoesNotChangeConclusions:
    """S07/S08/S09/S12b must agree with their clean counterparts."""

    def test_duplicates_do_not_change_the_timeline(self) -> None:
        clean = only_order("S01")
        duplicated = only_order("S07")

        assert [e.event_type for e in clean.events] == [e.event_type for e in duplicated.events]
        assert clean.state == duplicated.state
        assert len(clean.attempts) == len(duplicated.attempts)

    def test_duplicates_do_not_inflate_the_attempt_count(self) -> None:
        assert len(only_order("S07").attempts) == len(only_order("S01").attempts)

    def test_out_of_order_does_not_change_the_timeline(self) -> None:
        clean = only_order("S04")
        scrambled = only_order("S08")

        assert [e.event_type for e in clean.events] == [e.event_type for e in scrambled.events]
        assert clean.state == scrambled.state
        assert len(clean.failed_attempts) == len(scrambled.failed_attempts)

    def test_delay_does_not_change_the_timeline(self) -> None:
        clean = only_order("S04")
        delayed = only_order("S09")

        assert [e.occurred_at for e in clean.events] == [e.occurred_at for e in delayed.events]
        assert clean.state == delayed.state

    def test_missing_event_preserves_the_attempt_count(self) -> None:
        """Gap inference is what makes this hold."""
        assert len(only_order("S12b").attempts) == len(only_order("S04").attempts)

    def test_missing_event_preserves_the_failure_count(self) -> None:
        assert len(only_order("S12b").failed_attempts) == len(only_order("S04").failed_attempts)


class TestIntegrityFlags:
    def test_clean_scenario_is_clean(self) -> None:
        assert only_order("S01").integrity.is_clean

    def test_duplicates_are_counted(self) -> None:
        assert only_order("S07").integrity.duplicate_deliveries == 2

    def test_out_of_order_is_counted(self) -> None:
        assert only_order("S08").integrity.out_of_order_deliveries > 0

    def test_delay_is_measured(self) -> None:
        assert only_order("S09").integrity.max_delivery_lag_seconds >= 6 * 60 * 60

    def test_normal_delivery_lag_stays_small(self) -> None:
        assert only_order("S01").integrity.max_delivery_lag_seconds <= 60

    def test_gap_is_counted(self) -> None:
        assert only_order("S12b").integrity.inferred_gaps == 1

    def test_pathological_scenarios_are_not_clean(self) -> None:
        for scenario in ("S07", "S08", "S12b"):
            assert not only_order(scenario).integrity.is_clean

    def test_integrity_is_measured_against_delivery_not_causal_order(self) -> None:
        """Sorting first would hide out-of-order arrival entirely."""
        events = scenario_events("S04")
        assert reconstruct_order(ORDER_ID, events).integrity.out_of_order_deliveries == 0
        assert (
            reconstruct_order(ORDER_ID, reversed_delivery(events)).integrity.out_of_order_deliveries
            > 0
        )


class TestMerchantReconstruction:
    def test_groups_events_by_order(self) -> None:
        timeline = reconstruct_merchant(merchant_id_of("S14"), scenario_events("S14"))
        assert len(timeline.orders) == 20

    def test_subscription_events_become_subscription_timelines(self) -> None:
        timeline = reconstruct_merchant(merchant_id_of("S06"), scenario_events("S06"))
        assert timeline.orders == ()
        assert len(timeline.subscriptions) == 1

    def test_order_lookup(self) -> None:
        timeline = reconstruct_merchant(merchant_id_of("S04"), scenario_events("S04"))
        found = timeline.order(timeline.orders[0].order_id)
        assert found is not None

    def test_unknown_order_lookup_returns_none(self) -> None:
        timeline = reconstruct_merchant(merchant_id_of("S04"), scenario_events("S04"))
        assert timeline.order(ORDER_ID) is None

    def test_is_deterministic_across_repeated_runs(self) -> None:
        events = scenario_events("S14")
        first = reconstruct_merchant(merchant_id_of("S14"), events)
        second = reconstruct_merchant(merchant_id_of("S14"), events)

        assert [o.order_id for o in first.orders] == [o.order_id for o in second.orders]
        assert [o.state for o in first.orders] == [o.state for o in second.orders]


class TestTimelineView:
    def test_pairs_causal_and_delivery_positions(self) -> None:
        rows = timeline_view(only_order("S08"))
        assert [r["causal_position"] for r in rows] == list(range(1, len(rows) + 1))
        assert [r["delivery_position"] for r in rows] != [r["causal_position"] for r in rows]

    def test_reports_delay(self) -> None:
        rows = timeline_view(only_order("S09"))
        assert max(r["delay_seconds"] for r in rows) >= 6 * 60 * 60

    def test_clean_timeline_has_matching_positions(self) -> None:
        rows = timeline_view(only_order("S01"))
        assert [r["causal_position"] for r in rows] == [r["delivery_position"] for r in rows]


class TestEmptyInput:
    def test_reconstructing_zero_events_is_refused(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="zero events"):
            reconstruct_order(ORDER_ID, [])

    def test_merchant_with_no_events_is_empty(self) -> None:
        timeline = reconstruct_merchant(merchant_id_of("S01"), [])
        assert timeline.orders == ()
        assert timeline.subscriptions == ()


class TestEventTypeCoverage:
    def test_every_simulator_event_type_is_handled(self) -> None:
        """Reconstruction must not silently ignore a vocabulary member."""
        seen: set[str] = set()
        for scenario in ("S01", "S04", "S05", "S06", "S11", "S12", "S13"):
            seen.update(e.event_type for e in scenario_events(scenario))

        # Every type the simulator emits should be a known EventType.
        assert seen <= set(EventType.values())
        assert len(seen) >= 12
