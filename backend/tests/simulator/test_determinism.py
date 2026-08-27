"""Deterministic replay.

The contract: (scenario, seed, params, generator_version) -> identical output.
"""

from __future__ import annotations

import pytest
from simulator import GENERATOR_VERSION, simulate
from simulator.config import ScenarioParams
from simulator.serialization import canonical_json, events_jsonl, fixture_to_dict

from .conftest import SEED


class TestReplay:
    def test_same_seed_same_checksum(self, scenario_id: str) -> None:
        assert (
            simulate(scenario_id, seed=SEED).manifest.checksum
            == simulate(scenario_id, seed=SEED).manifest.checksum
        )

    def test_same_seed_identical_fixture_bytes(self, scenario_id: str) -> None:
        a = canonical_json(fixture_to_dict(simulate(scenario_id, seed=SEED)))
        b = canonical_json(fixture_to_dict(simulate(scenario_id, seed=SEED)))
        assert a == b

    def test_same_seed_identical_event_stream(self, scenario_id: str) -> None:
        assert events_jsonl(simulate(scenario_id, seed=SEED)) == events_jsonl(
            simulate(scenario_id, seed=SEED)
        )

    def test_repeated_runs_are_stable(self, scenario_id: str) -> None:
        checksums = {simulate(scenario_id, seed=SEED).manifest.checksum for _ in range(5)}
        assert len(checksums) == 1

    def test_generation_order_does_not_matter(self) -> None:
        """Generating other scenarios first must not change a scenario's output."""
        alone = simulate("S04", seed=SEED).manifest.checksum
        for other in ("S01", "S02", "S14", "S06"):
            simulate(other, seed=SEED)
        assert simulate("S04", seed=SEED).manifest.checksum == alone


class TestSeedSensitivity:
    def test_different_seed_different_checksum(self, scenario_id: str) -> None:
        assert (
            simulate(scenario_id, seed=SEED).manifest.checksum
            != simulate(scenario_id, seed=SEED + 1).manifest.checksum
        )

    def test_different_seed_still_valid(self, scenario_id: str) -> None:
        """A different seed must produce different but equally valid data."""
        result = simulate(scenario_id, seed=12_345)
        assert result.deliveries
        for delivery in result.deliveries:
            assert delivery.event.received_at >= delivery.event.occurred_at

    def test_different_seeds_change_identifiers(self) -> None:
        a = simulate("S04", seed=1)
        b = simulate("S04", seed=2)
        assert a.entities.orders[0].id != b.entities.orders[0].id
        assert a.deliveries[0].event.external_event_id != b.deliveries[0].event.external_event_id

    def test_different_seeds_change_amounts(self) -> None:
        """Seed sensitivity must reach real values, not just identifiers."""
        amounts = {simulate("S04", seed=seed).entities.orders[0].amount for seed in range(12)}
        assert len(amounts) > 1


class TestManifest:
    def test_records_generator_version(self, scenario_id: str) -> None:
        assert simulate(scenario_id, seed=SEED).manifest.generator_version == GENERATOR_VERSION

    def test_records_seed_and_scenario(self) -> None:
        manifest = simulate("S04", seed=SEED).manifest
        assert manifest.seed == SEED
        assert manifest.scenario_id == "S04"

    def test_checksum_is_sha256_prefixed(self, scenario_id: str) -> None:
        checksum = simulate(scenario_id, seed=SEED).manifest.checksum
        assert checksum.startswith("sha256:")
        assert len(checksum) == len("sha256:") + 64

    def test_counts_match_reality(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        counts = result.manifest.counts
        assert counts["orders"] == len(result.entities.orders)
        assert counts["customers"] == len(result.entities.customers)
        assert counts["payment_attempts"] == len(result.entities.payment_attempts)
        assert counts["events_emitted"] == len(result.deliveries)


class TestParameters:
    def test_params_change_output(self) -> None:
        default = simulate("S04", seed=SEED)
        more = simulate("S04", seed=SEED, params=ScenarioParams(attempt_count=5))
        assert more.manifest.checksum != default.manifest.checksum
        assert len(more.entities.payment_attempts) == 5

    def test_same_params_same_output(self) -> None:
        params = ScenarioParams(attempt_count=4)
        assert (
            simulate("S04", seed=SEED, params=params).manifest.checksum
            == simulate("S04", seed=SEED, params=params).manifest.checksum
        )

    @pytest.mark.parametrize("count", [1, 2, 3, 5])
    def test_attempt_count_honoured(self, count: int) -> None:
        result = simulate("S04", seed=SEED, params=ScenarioParams(attempt_count=count))
        assert len(result.entities.payment_attempts) == count
