"""Ingestion schema validation. Hermetic — no database.

The most important test in this file is the ground-truth rejection: it is the
first of three layers keeping evaluation answers out of detection input.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.ingestion import EventIn, SimulationIngestRequest
from tests.ingestion.conftest import full_fixture, ingest_payload

ALL_SCENARIOS = ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08", "S09", "S10")


def _first_event(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["deliveries"][0]["event"]


class TestGroundTruthRejection:
    """Layer one of three. Detection must not be able to read the answer."""

    def test_ground_truth_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ground_truth must not be submitted"):
            SimulationIngestRequest.model_validate(full_fixture("S04"))

    def test_rejection_names_the_reason(self) -> None:
        with pytest.raises(ValidationError, match="evaluation data"):
            SimulationIngestRequest.model_validate(full_fixture("S04"))

    def test_empty_ground_truth_is_still_rejected(self) -> None:
        payload = ingest_payload("S04")
        payload["ground_truth"] = {}
        with pytest.raises(ValidationError, match="ground_truth"):
            SimulationIngestRequest.model_validate(payload)

    def test_stripped_payload_is_accepted(self) -> None:
        assert SimulationIngestRequest.model_validate(ingest_payload("S04"))

    def test_unknown_top_level_key_is_rejected(self) -> None:
        payload = ingest_payload("S04")
        payload["expected_risks"] = []
        with pytest.raises(ValidationError):
            SimulationIngestRequest.model_validate(payload)

    @pytest.mark.parametrize("key", ["risk_type", "amount_at_risk", "narrative", "scenario_id"])
    def test_forbidden_payload_key_is_rejected(self, key: str) -> None:
        payload = ingest_payload("S04")
        _first_event(payload)["payload"][key] = "leak"
        with pytest.raises(ValidationError, match="evaluation-only keys"):
            SimulationIngestRequest.model_validate(payload)


class TestScenarioAcceptance:
    @pytest.mark.parametrize("scenario", ALL_SCENARIOS)
    def test_every_scenario_parses(self, scenario: str) -> None:
        request = SimulationIngestRequest.model_validate(ingest_payload(scenario))
        assert request.deliveries

    def test_manifest_is_accepted_and_carries_no_expectations(self) -> None:
        request = SimulationIngestRequest.model_validate(ingest_payload("S04"))
        assert request.manifest is not None
        assert "expected_risks" not in request.manifest
        assert request.manifest["scenario_id"] == "S04"

    def test_subscription_events_have_no_order(self) -> None:
        request = SimulationIngestRequest.model_validate(ingest_payload("S06"))
        assert all(d.event.order_id is None for d in request.deliveries)


class TestEventValidation:
    def test_unknown_event_type_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        _first_event(payload)["event_type"] = "payment.exploded"
        with pytest.raises(ValidationError, match="unknown event_type"):
            SimulationIngestRequest.model_validate(payload)

    def test_naive_occurred_at_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        _first_event(payload)["occurred_at"] = "2026-01-01T00:00:00"
        with pytest.raises(ValidationError, match="timezone-aware"):
            SimulationIngestRequest.model_validate(payload)

    def test_received_before_occurred_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        event = _first_event(payload)
        event["received_at"] = "2025-12-31T23:00:00Z"
        with pytest.raises(ValidationError, match="precedes occurred_at"):
            SimulationIngestRequest.model_validate(payload)

    def test_equal_timestamps_are_accepted(self) -> None:
        payload = ingest_payload("S01")
        event = _first_event(payload)
        event["received_at"] = event["occurred_at"]
        assert SimulationIngestRequest.model_validate(payload)

    def test_offset_timezone_is_normalised_to_utc(self) -> None:
        event = EventIn.model_validate(
            {
                **_first_event(ingest_payload("S01")),
                "occurred_at": "2026-01-01T05:30:00+05:30",
                "received_at": "2026-01-01T05:30:00+05:30",
            }
        )
        assert event.occurred_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert event.occurred_at.utcoffset() == timedelta(0)

    def test_oversized_external_event_id_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        _first_event(payload)["external_event_id"] = "x" * 129
        with pytest.raises(ValidationError):
            SimulationIngestRequest.model_validate(payload)

    def test_empty_external_event_id_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        _first_event(payload)["external_event_id"] = ""
        with pytest.raises(ValidationError):
            SimulationIngestRequest.model_validate(payload)

    def test_float_money_in_payload_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        _first_event(payload)["payload"]["amount_minor"] = 4999.5
        with pytest.raises(ValidationError, match="integer count of minor units"):
            SimulationIngestRequest.model_validate(payload)

    def test_negative_money_in_payload_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        _first_event(payload)["payload"]["amount_minor"] = -1
        with pytest.raises(ValidationError, match="non-negative"):
            SimulationIngestRequest.model_validate(payload)

    def test_bool_is_not_accepted_as_money(self) -> None:
        payload = ingest_payload("S01")
        _first_event(payload)["payload"]["amount_minor"] = True
        with pytest.raises(ValidationError, match="integer count of minor units"):
            SimulationIngestRequest.model_validate(payload)


class TestEntityValidation:
    def test_invalid_order_status_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["orders"][0]["status"] = "exploded"
        with pytest.raises(ValidationError, match="invalid order status"):
            SimulationIngestRequest.model_validate(payload)

    def test_invalid_payment_status_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["payment_attempts"][0]["status"] = "vibes"
        with pytest.raises(ValidationError, match="invalid payment status"):
            SimulationIngestRequest.model_validate(payload)

    def test_invalid_payment_method_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["payment_attempts"][0]["payment_method"] = "barter"
        with pytest.raises(ValidationError, match="invalid payment method"):
            SimulationIngestRequest.model_validate(payload)

    def test_fractional_float_order_amount_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["orders"][0]["amount"] = 4999.99
        with pytest.raises(ValidationError):
            SimulationIngestRequest.model_validate(payload)

    def test_whole_valued_float_amount_is_also_rejected(self) -> None:
        """The interesting case: 4999.0 is lossless, and still refused.

        Money fields are StrictInt precisely so a float cannot be quietly
        coerced into a money path (ADR 0001).
        """
        payload = ingest_payload("S01")
        payload["entities"]["orders"][0]["amount"] = 4999.0
        with pytest.raises(ValidationError):
            SimulationIngestRequest.model_validate(payload)

    def test_whole_valued_float_lifetime_value_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["customers"][0]["lifetime_value"] = 100000.0
        with pytest.raises(ValidationError):
            SimulationIngestRequest.model_validate(payload)

    def test_numeric_string_amount_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["orders"][0]["amount"] = "4999"
        with pytest.raises(ValidationError):
            SimulationIngestRequest.model_validate(payload)

    def test_negative_lifetime_value_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["customers"][0]["lifetime_value"] = -100
        with pytest.raises(ValidationError, match="non-negative"):
            SimulationIngestRequest.model_validate(payload)

    def test_attempt_number_below_one_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["payment_attempts"][0]["attempt_number"] = 0
        with pytest.raises(ValidationError):
            SimulationIngestRequest.model_validate(payload)

    def test_merchant_external_ref_is_accepted_and_ignored(self) -> None:
        """The Phase 1 merchants table has no column for it."""
        request = SimulationIngestRequest.model_validate(ingest_payload("S01"))
        assert request.entities.merchants[0].external_ref is not None

    def test_bad_currency_length_is_rejected(self) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["orders"][0]["currency"] = "RUPEE"
        with pytest.raises(ValidationError):
            SimulationIngestRequest.model_validate(payload)


class TestAllOrNothing:
    def test_one_bad_event_rejects_the_whole_batch(self) -> None:
        """A partial ingest would leave a torn timeline."""
        payload = ingest_payload("S14")
        original = copy.deepcopy(payload)
        payload["deliveries"][5]["event"]["event_type"] = "nope"

        with pytest.raises(ValidationError):
            SimulationIngestRequest.model_validate(payload)

        assert SimulationIngestRequest.model_validate(original)

    def test_envelope_is_parsed_but_separate_from_the_event(self) -> None:
        request = SimulationIngestRequest.model_validate(ingest_payload("S07"))
        delivery = request.deliveries[0]
        assert not hasattr(delivery.event, "is_duplicate")
        assert hasattr(delivery.envelope, "is_duplicate")
