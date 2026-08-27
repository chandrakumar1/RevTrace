"""Invalid input and invalid output are rejected loudly."""

from __future__ import annotations

import pytest
from simulator import simulate
from simulator.config import ScenarioParams
from simulator.events import EventIdFactory, build_event
from simulator.models import EntitySet, ExpectedRisk, GroundTruth
from simulator.rng import DeterministicRng
from simulator.validation import (
    InvalidSeedError,
    InvariantViolation,
    SimulationError,
    UnknownScenarioError,
    validate_ground_truth,
    validate_seed,
)


class TestScenarioValidation:
    def test_unknown_scenario_rejected(self) -> None:
        with pytest.raises(UnknownScenarioError):
            simulate("NOPE", seed=1)

    def test_unknown_scenario_lists_known_ids(self) -> None:
        with pytest.raises(UnknownScenarioError, match="S04"):
            simulate("NOPE", seed=1)

    def test_empty_scenario_rejected(self) -> None:
        with pytest.raises(UnknownScenarioError):
            simulate("", seed=1)

    def test_errors_share_a_base_class(self) -> None:
        assert issubclass(UnknownScenarioError, SimulationError)
        assert issubclass(InvalidSeedError, SimulationError)
        assert issubclass(InvariantViolation, SimulationError)


class TestSeedValidation:
    def test_negative_seed_rejected(self) -> None:
        with pytest.raises(InvalidSeedError):
            simulate("S01", seed=-1)

    def test_string_seed_rejected(self) -> None:
        with pytest.raises(InvalidSeedError):
            simulate("S01", seed="42")  # type: ignore[arg-type]

    def test_float_seed_rejected(self) -> None:
        with pytest.raises(InvalidSeedError):
            simulate("S01", seed=1.5)  # type: ignore[arg-type]

    def test_bool_seed_rejected(self) -> None:
        with pytest.raises(InvalidSeedError):
            validate_seed(True)

    def test_zero_seed_accepted(self) -> None:
        assert simulate("S01", seed=0).deliveries


class TestParamValidation:
    def test_negative_attempt_count_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScenarioParams(attempt_count=-1)

    def test_float_delay_rejected(self) -> None:
        with pytest.raises(TypeError):
            ScenarioParams(delay_seconds=1.5)  # type: ignore[arg-type]

    def test_bad_currency_rejected(self) -> None:
        with pytest.raises(ValueError, match="ISO 4217"):
            ScenarioParams(currency="RUPEE")

    def test_defaults_are_valid(self) -> None:
        assert ScenarioParams().currency == "INR"


class TestEventValidation:
    def test_received_before_occurred_rejected(self) -> None:
        from simulator.clock import SimulationClock

        rng = DeterministicRng(1)
        clock = SimulationClock()
        with pytest.raises(ValueError, match="must not precede"):
            build_event(
                rng,
                EventIdFactory("SX", 1),
                merchant_id=rng.uuid(),
                event_type=__import__(
                    "app.models.enums", fromlist=["EventType"]
                ).EventType.ORDER_CREATED,
                occurred_at=clock.at(100),
                received_at=clock.at(50),
                payload={},
            )

    def test_ground_truth_key_in_payload_rejected(self) -> None:
        from simulator.clock import SimulationClock

        from app.models.enums import EventType

        rng = DeterministicRng(1)
        clock = SimulationClock()
        with pytest.raises(ValueError, match="ground-truth keys"):
            build_event(
                rng,
                EventIdFactory("SX", 1),
                merchant_id=rng.uuid(),
                event_type=EventType.ORDER_CREATED,
                occurred_at=clock.at(0),
                received_at=clock.at(1),
                payload={"risk_type": "repeated_payment_failure"},
            )


class TestGroundTruthValidation:
    def test_invalid_risk_type_rejected(self) -> None:
        truth = GroundTruth(
            expected_risks=(
                ExpectedRisk(
                    risk_type="not_a_real_risk",
                    amount_at_risk=100,
                    currency="INR",
                    order_ref=None,
                    reason="test",
                ),
            )
        )
        with pytest.raises(InvariantViolation, match="invalid risk_type"):
            validate_ground_truth(truth)

    def test_negative_amount_rejected(self) -> None:
        truth = GroundTruth(
            expected_risks=(
                ExpectedRisk(
                    risk_type="checkout_abandonment",
                    amount_at_risk=-5,
                    currency="INR",
                    order_ref=None,
                    reason="test",
                ),
            )
        )
        with pytest.raises(InvariantViolation, match="non-negative"):
            validate_ground_truth(truth)

    def test_persisted_cannot_exceed_emitted(self) -> None:
        truth = GroundTruth(emitted_event_count=3, expected_persisted_event_count=5)
        with pytest.raises(InvariantViolation, match="cannot exceed"):
            validate_ground_truth(truth)

    def test_empty_ground_truth_is_valid(self) -> None:
        validate_ground_truth(GroundTruth())


class TestEntityValidation:
    def test_empty_entity_set_is_valid(self) -> None:
        from simulator.validation import validate_entities

        validate_entities(EntitySet())
