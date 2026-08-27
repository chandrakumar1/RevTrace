"""Fixtures for the simulator suite.

Every test here is hermetic: no database connection, no network, no filesystem
writes outside pytest's own tmp_path.
"""

from __future__ import annotations

import pytest
from simulator.scenarios import all_scenarios

#: Every registered scenario id, for parametrization.
ALL_SCENARIO_IDS = tuple(spec.id for spec in all_scenarios())

#: A representative seed used across the suite.
SEED = 42


@pytest.fixture(params=ALL_SCENARIO_IDS)
def scenario_id(request: pytest.FixtureRequest) -> str:
    return str(request.param)
