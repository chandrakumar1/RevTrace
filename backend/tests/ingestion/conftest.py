"""Fixtures shared by the ingestion tests. Hermetic — no database."""

from __future__ import annotations

from typing import Any

import pytest
from simulator import simulate
from simulator.serialization import fixture_to_dict

SEED = 42


def ingest_payload(scenario: str, seed: int = SEED) -> dict[str, Any]:
    """A simulator fixture with its answer key stripped, as a client must send."""
    fixture = fixture_to_dict(simulate(scenario, seed=seed))
    return {key: value for key, value in fixture.items() if key != "ground_truth"}


def full_fixture(scenario: str, seed: int = SEED) -> dict[str, Any]:
    """The complete fixture, ground truth included — for rejection tests."""
    return fixture_to_dict(simulate(scenario, seed=seed))


@pytest.fixture
def healthy_payload() -> dict[str, Any]:
    return ingest_payload("S01")


@pytest.fixture
def duplicate_payload() -> dict[str, Any]:
    return ingest_payload("S07")
