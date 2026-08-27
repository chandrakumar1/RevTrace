"""Helpers for reconstruction tests. Hermetic — no database.

Timelines come either from the Phase 2 simulator (pure, deterministic) or from
`EventDouble`, a minimal structural stand-in for edge cases the simulator does
not produce.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from simulator import simulate
from simulator.models import SimulationResult

from app.services.tracing.state import EventLike

SEED = 42
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

MERCHANT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORDER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
CUSTOMER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


@dataclass
class EventDouble:
    """A minimal object satisfying `EventLike`."""

    external_event_id: str
    event_type: str
    occurred_at: datetime
    received_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    merchant_id: uuid.UUID = MERCHANT_ID
    customer_id: uuid.UUID | None = CUSTOMER_ID
    order_id: uuid.UUID | None = ORDER_ID


def event(
    event_type: str,
    occurred: int,
    *,
    received: int | None = None,
    external_id: str | None = None,
    order_id: uuid.UUID | None = ORDER_ID,
    **payload: Any,
) -> EventDouble:
    """Build an event at `occurred` seconds past the epoch."""
    arrival = occurred if received is None else received
    return EventDouble(
        external_event_id=external_id or f"evt_{event_type}_{occurred}",
        event_type=event_type,
        occurred_at=EPOCH + timedelta(seconds=occurred),
        received_at=EPOCH + timedelta(seconds=arrival),
        payload=dict(payload),
        order_id=order_id,
    )


def scenario_events(scenario: str, seed: int = SEED) -> list[EventLike]:
    """The delivery stream for a scenario, in arrival order."""
    return [delivery.event for delivery in simulate(scenario, seed=seed).deliveries]


def scenario_result(scenario: str, seed: int = SEED) -> SimulationResult:
    return simulate(scenario, seed=seed)


def merchant_id_of(scenario: str, seed: int = SEED) -> uuid.UUID:
    return simulate(scenario, seed=seed).entities.merchants[0].id


def only_order(scenario: str, seed: int = SEED) -> Any:
    """Reconstruct a scenario's single order timeline."""
    from app.services.tracing.reconstruction import reconstruct_merchant

    timeline = reconstruct_merchant(merchant_id_of(scenario, seed), scenario_events(scenario, seed))
    assert timeline.orders, f"{scenario} produced no order timeline"
    return timeline.orders[0]


def reversed_delivery(events: Sequence[EventLike]) -> list[EventLike]:
    return list(reversed(list(events)))
