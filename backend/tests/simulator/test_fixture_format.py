"""Fixture serialization — the contract the frontend and Phase 3 build against."""

from __future__ import annotations

import json

from simulator import simulate
from simulator.serialization import (
    canonical_json,
    compute_checksum,
    events_jsonl,
    fixture_to_dict,
    frontend_view,
)

from .conftest import SEED


class TestFixtureJson:
    def test_has_the_four_top_level_sections(self, scenario_id: str) -> None:
        fixture = fixture_to_dict(simulate(scenario_id, seed=SEED))
        assert set(fixture) == {"manifest", "entities", "deliveries", "ground_truth"}

    def test_is_json_serializable(self, scenario_id: str) -> None:
        json.dumps(fixture_to_dict(simulate(scenario_id, seed=SEED)))

    def test_round_trips(self, scenario_id: str) -> None:
        fixture = fixture_to_dict(simulate(scenario_id, seed=SEED))
        assert json.loads(json.dumps(fixture)) == fixture

    def test_canonical_form_is_byte_stable(self, scenario_id: str) -> None:
        a = canonical_json(fixture_to_dict(simulate(scenario_id, seed=SEED)))
        b = canonical_json(fixture_to_dict(simulate(scenario_id, seed=SEED)))
        assert a == b

    def test_timestamps_serialize_with_z_suffix(self, scenario_id: str) -> None:
        fixture = fixture_to_dict(simulate(scenario_id, seed=SEED))
        for delivery in fixture["deliveries"]:
            assert delivery["event"]["occurred_at"].endswith("Z")
            assert delivery["event"]["received_at"].endswith("Z")

    def test_uuids_serialize_as_strings(self, scenario_id: str) -> None:
        import uuid

        fixture = fixture_to_dict(simulate(scenario_id, seed=SEED))
        for delivery in fixture["deliveries"]:
            uuid.UUID(delivery["event"]["id"])

    def test_money_stays_integer_on_the_wire(self, scenario_id: str) -> None:
        fixture = fixture_to_dict(simulate(scenario_id, seed=SEED))
        for order in fixture["entities"]["orders"]:
            assert isinstance(order["amount"], int)


class TestChecksum:
    def test_is_stable_for_identical_input(self, scenario_id: str) -> None:
        a = simulate(scenario_id, seed=SEED)
        b = simulate(scenario_id, seed=SEED)
        assert compute_checksum(a.entities, a.deliveries, a.ground_truth) == compute_checksum(
            b.entities, b.deliveries, b.ground_truth
        )

    def test_matches_the_manifest(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        recomputed = compute_checksum(result.entities, result.deliveries, result.ground_truth)
        assert recomputed == result.manifest.checksum


class TestEventsJsonl:
    def test_one_line_per_delivery(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        lines = events_jsonl(result).strip().split("\n")
        assert len(lines) == len(result.deliveries)

    def test_each_line_is_valid_json(self, scenario_id: str) -> None:
        for line in events_jsonl(simulate(scenario_id, seed=SEED)).strip().split("\n"):
            parsed = json.loads(line)
            assert set(parsed) == {"delivery", "event"}

    def test_delivery_metadata_is_outside_the_event(self, scenario_id: str) -> None:
        """Envelope fields must never contaminate the persisted event row."""
        for line in events_jsonl(simulate(scenario_id, seed=SEED)).strip().split("\n"):
            event = json.loads(line)["event"]
            for envelope_field in ("sequence", "delivery_attempt", "is_duplicate"):
                assert envelope_field not in event

    def test_event_fields_match_the_phase_1_columns(self, scenario_id: str) -> None:
        expected = {
            "id",
            "merchant_id",
            "customer_id",
            "order_id",
            "external_event_id",
            "event_type",
            "payload",
            "occurred_at",
            "received_at",
        }
        for line in events_jsonl(simulate(scenario_id, seed=SEED)).strip().split("\n"):
            assert set(json.loads(line)["event"]) == expected

    def test_arrival_order_is_preserved(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        lines = events_jsonl(result).strip().split("\n")
        sequences = [json.loads(line)["delivery"]["sequence"] for line in lines]
        assert sequences == sorted(sequences)


class TestFrontendView:
    def test_has_the_documented_top_level_keys(self, scenario_id: str) -> None:
        view = frontend_view(simulate(scenario_id, seed=SEED))
        assert set(view) == {
            "summary",
            "orders",
            "timeline",
            "payment_attempts",
            "revenue_risk",
            "recovery_state",
        }

    def test_revenue_risk_is_explicitly_null_in_phase_2(self, scenario_id: str) -> None:
        assert frontend_view(simulate(scenario_id, seed=SEED))["revenue_risk"] is None

    def test_recovery_state_is_explicitly_null_in_phase_2(self, scenario_id: str) -> None:
        assert frontend_view(simulate(scenario_id, seed=SEED))["recovery_state"] is None

    def test_is_json_serializable(self, scenario_id: str) -> None:
        json.dumps(frontend_view(simulate(scenario_id, seed=SEED)))

    def test_timeline_matches_the_delivery_stream(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        assert len(frontend_view(result)["timeline"]) == len(result.deliveries)

    def test_timeline_entries_carry_delivery_flags(self, scenario_id: str) -> None:
        for entry in frontend_view(simulate(scenario_id, seed=SEED))["timeline"]:
            assert isinstance(entry["is_duplicate"], bool)
            assert isinstance(entry["is_out_of_order"], bool)
            assert isinstance(entry["is_delayed"], bool)
            assert entry["summary"]

    def test_summary_money_is_integer_minor_units(self, scenario_id: str) -> None:
        summary = frontend_view(simulate(scenario_id, seed=SEED))["summary"]
        assert isinstance(summary["amount_captured_minor"], int)
        assert isinstance(summary["amount_at_risk_minor"], int)

    def test_attempts_are_sorted_by_time(self, scenario_id: str) -> None:
        attempts = frontend_view(simulate(scenario_id, seed=SEED))["payment_attempts"]
        times = [a["attempted_at"] for a in attempts]
        assert times == sorted(times)

    def test_duplicates_are_visible_to_the_ui(self) -> None:
        view = frontend_view(simulate("S07", seed=SEED))
        assert any(entry["is_duplicate"] for entry in view["timeline"])

    def test_at_risk_total_matches_ground_truth(self) -> None:
        result = simulate("S04", seed=SEED)
        view = frontend_view(result)
        assert view["summary"]["amount_at_risk_minor"] == sum(
            risk.amount_at_risk for risk in result.ground_truth.expected_risks
        )

    def test_no_ground_truth_narrative_leaks_into_the_view(self, scenario_id: str) -> None:
        """The frontend view is derived from events, not from the answer key."""
        result = simulate(scenario_id, seed=SEED)
        rendered = json.dumps(frontend_view(result))
        if result.ground_truth.narrative:
            assert result.ground_truth.narrative not in rendered
