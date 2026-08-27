"""Simulation time: UTC awareness and the received_at >= occurred_at invariant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from simulator import simulate
from simulator.clock import SIMULATION_EPOCH, SimulationClock, is_utc

from .conftest import SEED


class TestEpoch:
    def test_epoch_is_timezone_aware_utc(self) -> None:
        assert is_utc(SIMULATION_EPOCH)

    def test_epoch_is_a_fixed_constant(self) -> None:
        assert SIMULATION_EPOCH == datetime(2026, 1, 1, tzinfo=UTC)


class TestClock:
    def test_at_returns_utc(self) -> None:
        assert is_utc(SimulationClock().at(3600))

    def test_at_applies_offset(self) -> None:
        clock = SimulationClock()
        assert clock.at(3600) - clock.epoch == timedelta(hours=1)

    def test_zero_offset_is_epoch(self) -> None:
        assert SimulationClock().at(0) == SIMULATION_EPOCH

    def test_rejects_naive_epoch(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            SimulationClock(datetime(2026, 1, 1))

    def test_rejects_non_utc_epoch(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        with pytest.raises(ValueError, match="UTC"):
            SimulationClock(datetime(2026, 1, 1, tzinfo=ist))

    def test_rejects_float_offset(self) -> None:
        with pytest.raises(TypeError):
            SimulationClock().at(1.5)  # type: ignore[arg-type]

    def test_rejects_negative_offset(self) -> None:
        with pytest.raises(ValueError):
            SimulationClock().at(-1)


class TestIsUtc:
    def test_naive_is_not_utc(self) -> None:
        assert not is_utc(datetime(2026, 1, 1))

    def test_offset_timezone_is_not_utc(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        assert not is_utc(datetime(2026, 1, 1, tzinfo=ist))


class TestGeneratedTimestamps:
    """Applied across every scenario — the invariants that must never break."""

    def test_all_event_timestamps_are_utc(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        for delivery in result.deliveries:
            assert is_utc(delivery.event.occurred_at)
            assert is_utc(delivery.event.received_at)

    def test_received_at_never_precedes_occurred_at(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        for delivery in result.deliveries:
            assert delivery.event.received_at >= delivery.event.occurred_at

    def test_attempt_timestamps_are_utc(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        for attempt in result.entities.payment_attempts:
            assert is_utc(attempt.attempted_at)

    def test_all_times_at_or_after_epoch(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        for delivery in result.deliveries:
            assert delivery.event.occurred_at >= SIMULATION_EPOCH

    def test_manifest_window_brackets_the_events(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        occurred = [d.event.occurred_at for d in result.deliveries]
        assert result.manifest.window_start == min(occurred)
        assert result.manifest.window_end == max(occurred)
