"""Scenario framework.

A scenario is a pure function from a `BuildContext` to a `ScenarioOutput`. It
produces entities, a causally-ordered event list, a delivery plan, and ground
truth. It performs no I/O and touches no database.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from simulator.clock import SimulationClock
from simulator.config import ScenarioCategory, ScenarioParams
from simulator.delivery import DeliveryPlan
from simulator.events import EventIdFactory
from simulator.models import EntitySet, GroundTruth, SyntheticEvent
from simulator.rng import DeterministicRng


@dataclass(frozen=True, slots=True)
class BuildContext:
    """Everything a scenario builder is allowed to draw on.

    Sub-streams are pre-derived so that a scenario cannot accidentally share one
    generator across concerns and couple them together.
    """

    seed: int
    params: ScenarioParams
    clock: SimulationClock
    ids: EventIdFactory
    entity_rng: DeterministicRng
    timing_rng: DeterministicRng
    amount_rng: DeterministicRng
    delivery_rng: DeterministicRng

    @property
    def currency(self) -> str:
        return self.params.currency


@dataclass(frozen=True, slots=True)
class ScenarioOutput:
    """What a scenario builder returns."""

    entities: EntitySet
    #: Causally ordered by occurred_at. Delivery pathology is applied later.
    events: tuple[SyntheticEvent, ...]
    ground_truth: GroundTruth
    delivery_plan: DeliveryPlan = field(default_factory=DeliveryPlan)


ScenarioBuilder = Callable[[BuildContext], ScenarioOutput]


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    """Registry entry for one scenario."""

    id: str
    name: str
    category: ScenarioCategory
    description: str
    purpose: str
    builder: ScenarioBuilder
