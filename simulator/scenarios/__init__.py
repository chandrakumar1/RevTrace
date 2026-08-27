"""Scenario registry.

Adding a scenario means adding a `ScenarioSpec` to one of the category modules
and nothing else — the registry assembles itself from them.
"""

from __future__ import annotations

from simulator.config import ScenarioCategory
from simulator.scenarios import baseline, delivery_integrity, leak, reconciliation
from simulator.scenarios.base import BuildContext, ScenarioOutput, ScenarioSpec

_ALL_SPECS: tuple[ScenarioSpec, ...] = (
    *baseline.SPECS,
    *leak.SPECS,
    *delivery_integrity.SPECS,
    *reconciliation.SPECS,
)

SCENARIO_REGISTRY: dict[str, ScenarioSpec] = {spec.id: spec for spec in _ALL_SPECS}

if len(SCENARIO_REGISTRY) != len(_ALL_SPECS):  # pragma: no cover - import-time guard
    raise RuntimeError("duplicate scenario id in the registry")


def get_scenario(scenario_id: str) -> ScenarioSpec:
    """Look up a scenario by id, raising a helpful error if unknown."""
    from simulator.validation import UnknownScenarioError

    spec = SCENARIO_REGISTRY.get(scenario_id)
    if spec is None:
        known = ", ".join(sorted(SCENARIO_REGISTRY))
        raise UnknownScenarioError(f"unknown scenario {scenario_id!r}; known ids: {known}")
    return spec


def scenarios_by_category(category: ScenarioCategory) -> tuple[ScenarioSpec, ...]:
    return tuple(spec for spec in _ALL_SPECS if spec.category is category)


def all_scenarios() -> tuple[ScenarioSpec, ...]:
    return _ALL_SPECS


__all__ = [
    "BuildContext",
    "SCENARIO_REGISTRY",
    "ScenarioOutput",
    "ScenarioSpec",
    "all_scenarios",
    "get_scenario",
    "scenarios_by_category",
]
