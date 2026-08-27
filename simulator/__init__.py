"""RevTrace deterministic synthetic event simulator.

Public entry point::

    from simulator import simulate
    result = simulate("S04", seed=42)

`simulate()` is pure: it performs no I/O, opens no database connection, makes
no network call, and returns an in-memory `SimulationResult`. Persistence and
serialization are deliberately separate concerns.

The same (scenario, seed, params, generator_version) always produces the same
output, verified by the SHA-256 checksum in the manifest.
"""

from __future__ import annotations

from simulator.clock import SIMULATION_EPOCH, SimulationClock
from simulator.config import ScenarioCategory, ScenarioParams
from simulator.delivery import apply_delivery_plan
from simulator.events import EventIdFactory
from simulator.models import (
    EntitySet,
    EventDelivery,
    GroundTruth,
    SimulationManifest,
    SimulationResult,
    SyntheticEvent,
)
from simulator.rng import DeterministicRng
from simulator.scenarios import (
    SCENARIO_REGISTRY,
    all_scenarios,
    get_scenario,
    scenarios_by_category,
)
from simulator.scenarios.base import BuildContext
from simulator.serialization import compute_checksum
from simulator.validation import (
    InvalidSeedError,
    InvariantViolation,
    SimulationError,
    UnknownScenarioError,
    validate_deliveries,
    validate_entities,
    validate_ground_truth,
    validate_seed,
)
from simulator.version import GENERATOR_VERSION

__all__ = [
    "GENERATOR_VERSION",
    "SCENARIO_REGISTRY",
    "SIMULATION_EPOCH",
    "DeterministicRng",
    "EntitySet",
    "EventDelivery",
    "GroundTruth",
    "InvalidSeedError",
    "InvariantViolation",
    "ScenarioCategory",
    "ScenarioParams",
    "SimulationError",
    "SimulationManifest",
    "SimulationResult",
    "SyntheticEvent",
    "UnknownScenarioError",
    "all_scenarios",
    "get_scenario",
    "scenarios_by_category",
    "simulate",
]


def simulate(
    scenario: str,
    *,
    seed: int,
    params: ScenarioParams | None = None,
    epoch: object = None,
) -> SimulationResult:
    """Generate one deterministic synthetic history.

    Raises `UnknownScenarioError` for an unregistered scenario id,
    `InvalidSeedError` for a non-integer or negative seed, and
    `InvariantViolation` if generated output would violate a guarantee.
    """
    spec = get_scenario(scenario)
    validated_seed = validate_seed(seed)
    resolved_params = params or ScenarioParams()

    root = DeterministicRng(validated_seed, label=spec.id)
    clock = SimulationClock() if epoch is None else SimulationClock(epoch)  # type: ignore[arg-type]

    ctx = BuildContext(
        seed=validated_seed,
        params=resolved_params,
        clock=clock,
        ids=EventIdFactory(spec.id, validated_seed),
        entity_rng=root.derive("entities"),
        timing_rng=root.derive("timing"),
        amount_rng=root.derive("amounts"),
        delivery_rng=root.derive("delivery"),
    )

    output = spec.builder(ctx)
    deliveries = apply_delivery_plan(output.events, output.delivery_plan)

    validate_entities(output.entities)
    validate_deliveries(deliveries)
    validate_ground_truth(output.ground_truth)

    if len(deliveries) != output.ground_truth.emitted_event_count:
        raise InvariantViolation(
            f"{spec.id}: ground truth claims {output.ground_truth.emitted_event_count} "
            f"emitted events but {len(deliveries)} were delivered"
        )

    occurred = [delivery.event.occurred_at for delivery in deliveries]
    checksum = compute_checksum(output.entities, deliveries, output.ground_truth)

    manifest = SimulationManifest(
        scenario_id=spec.id,
        scenario_name=spec.name,
        category=spec.category.value,
        seed=validated_seed,
        generator_version=GENERATOR_VERSION,
        epoch=clock.epoch,
        currency=resolved_params.currency,
        counts={
            "merchants": len(output.entities.merchants),
            "customers": len(output.entities.customers),
            "orders": len(output.entities.orders),
            "payment_attempts": len(output.entities.payment_attempts),
            "events_emitted": len(deliveries),
            "events_unique": output.ground_truth.expected_persisted_event_count,
        },
        window_start=min(occurred) if occurred else None,
        window_end=max(occurred) if occurred else None,
        checksum=checksum,
    )

    return SimulationResult(
        manifest=manifest,
        entities=output.entities,
        deliveries=deliveries,
        ground_truth=output.ground_truth,
    )
