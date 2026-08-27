"""Shared scenario construction helpers.

Keeps the individual scenario builders short enough to read as narratives.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.models.enums import EventType
from simulator.config import (
    NORMAL_DELIVERY_LAG_SECONDS,
    TYPICAL_LIFETIME_VALUE_PAISE,
    TYPICAL_ORDER_PAISE,
)
from simulator.entities import build_customer, build_merchant, build_order
from simulator.events import build_event
from simulator.models import (
    SyntheticCustomer,
    SyntheticEvent,
    SyntheticMerchant,
    SyntheticOrder,
)
from simulator.scenarios.base import BuildContext


@dataclass(slots=True)
class EventLog:
    """Accumulates causally-ordered events for one scenario."""

    ctx: BuildContext
    merchant: SyntheticMerchant
    events: list[SyntheticEvent] = field(default_factory=list)

    def add(
        self,
        event_type: EventType,
        occurred_offset: int,
        payload: dict[str, object],
        *,
        customer_id: uuid.UUID | None = None,
        order_id: uuid.UUID | None = None,
    ) -> SyntheticEvent:
        """Append an event at a causal offset, with a normal delivery lag."""
        low, high = NORMAL_DELIVERY_LAG_SECONDS
        lag = self.ctx.delivery_rng.randint(low, high)

        event = build_event(
            self.ctx.entity_rng,
            self.ctx.ids,
            merchant_id=self.merchant.id,
            event_type=event_type,
            occurred_at=self.ctx.clock.at(occurred_offset),
            received_at=self.ctx.clock.at(occurred_offset + lag),
            payload=payload,
            customer_id=customer_id,
            order_id=order_id,
        )
        self.events.append(event)
        return event

    def as_tuple(self) -> tuple[SyntheticEvent, ...]:
        return tuple(self.events)

    def index_of(self, external_event_id: str) -> int:
        for index, event in enumerate(self.events):
            if event.external_event_id == external_event_id:
                return index
        raise KeyError(f"no event with external_event_id {external_event_id!r}")


def base_actors(
    ctx: BuildContext,
    *,
    order_index: int = 1,
    customer_index: int = 1,
    amount_range: tuple[int, int, int] = TYPICAL_ORDER_PAISE,
    amount: int | None = None,
    contactable: bool = True,
    lifetime_value_range: tuple[int, int, int] | None = None,
) -> tuple[SyntheticMerchant, SyntheticCustomer, SyntheticOrder]:
    """Build the standard merchant / customer / order trio."""
    merchant = build_merchant(ctx.entity_rng, seed=ctx.seed, currency=ctx.currency)

    customer = build_customer(
        ctx.entity_rng,
        seed=ctx.seed,
        index=customer_index,
        merchant_id=merchant.id,
        contactable=contactable,
        lifetime_value_range=lifetime_value_range or TYPICAL_LIFETIME_VALUE_PAISE,
    )
    order = build_order(
        ctx.amount_rng,
        seed=ctx.seed,
        index=order_index,
        merchant_id=merchant.id,
        customer_id=customer.id,
        amount_range=amount_range,
        amount=amount,
        currency=ctx.currency,
    )
    return merchant, customer, order


def retry_gap(ctx: BuildContext) -> int:
    """Deterministic seconds between successive payment attempts."""
    from simulator.config import RETRY_GAP_SECONDS

    low, high = RETRY_GAP_SECONDS
    return ctx.timing_rng.randint(low, high)
