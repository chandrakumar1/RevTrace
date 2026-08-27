"""Helpers for engine tests. Hermetic — no database."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.services.tracing.reconstruction import reconstruct_merchant
from tests.tracing.conftest import merchant_id_of, scenario_events

SEED = 42

#: Fixed evaluation instant, well after every simulated scenario ends.
#: Detectors never read the clock, so this is supplied explicitly and the whole
#: suite stays reproducible.
AS_OF = datetime(2026, 6, 1, tzinfo=UTC)

#: An instant only seconds after the simulation epoch, for testing that
#: time-gated detectors stay silent before their window elapses.
AS_OF_IMMEDIATE = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)


def order_timeline(scenario: str, seed: int = SEED) -> Any:
    timeline = reconstruct_merchant(merchant_id_of(scenario, seed), scenario_events(scenario, seed))
    assert timeline.orders, f"{scenario} produced no order timeline"
    return timeline.orders[0]


def subscription_timeline(scenario: str, seed: int = SEED) -> Any:
    timeline = reconstruct_merchant(merchant_id_of(scenario, seed), scenario_events(scenario, seed))
    assert timeline.subscriptions, f"{scenario} produced no subscription timeline"
    return timeline.subscriptions[0]
