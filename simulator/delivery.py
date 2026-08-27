"""Delivery transforms.

Event generation produces a clean, causally-correct history. This module
corrupts the *delivery* of that history — duplicating, delaying, reordering,
dropping — without ever touching `occurred_at`.

That separation is the point: it makes "the timeline reconstructs correctly
despite pathological delivery" a property that can actually be asserted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from simulator.models import DeliveryEnvelope, EventDelivery, SyntheticEvent


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    """How a canonical event sequence should (mis)behave on delivery.

    Indices refer to positions in the canonical, causally-ordered event list.
    """

    #: index -> number of EXTRA deliveries beyond the first.
    duplicates: Mapping[int, int] = field(default_factory=dict)
    #: index -> extra delivery delay in seconds, added to received_at.
    delays: Mapping[int, int] = field(default_factory=dict)
    #: indices that are generated but never delivered.
    drops: frozenset[int] = frozenset()
    #: explicit delivery order of indices; None means causal order.
    reorder: tuple[int, ...] | None = None

    def validate(self, event_count: int) -> None:
        for label, indices in (
            ("duplicates", self.duplicates.keys()),
            ("delays", self.delays.keys()),
            ("drops", self.drops),
        ):
            for index in indices:
                if not 0 <= index < event_count:
                    raise ValueError(
                        f"{label} references event index {index}, "
                        f"outside range 0..{event_count - 1}"
                    )

        for index, count in self.duplicates.items():
            if count < 1:
                raise ValueError(f"duplicate count for index {index} must be >= 1, got {count}")

        for index, seconds in self.delays.items():
            if seconds < 0:
                raise ValueError(f"delay for index {index} must be non-negative, got {seconds}")

        if self.reorder is not None:
            expected = set(range(event_count)) - set(self.drops)
            if set(self.reorder) != expected:
                raise ValueError("reorder must be a permutation of all non-dropped event indices")


def _delayed(event: SyntheticEvent, extra_seconds: int) -> SyntheticEvent:
    """Return a copy with a later received_at. occurred_at is untouched."""
    return SyntheticEvent(
        id=event.id,
        merchant_id=event.merchant_id,
        customer_id=event.customer_id,
        order_id=event.order_id,
        external_event_id=event.external_event_id,
        event_type=event.event_type,
        payload=event.payload,
        occurred_at=event.occurred_at,
        received_at=event.received_at + timedelta(seconds=extra_seconds),
    )


def apply_delivery_plan(
    events: Sequence[SyntheticEvent], plan: DeliveryPlan
) -> tuple[EventDelivery, ...]:
    """Turn a canonical event sequence into a delivery stream.

    Duplicates are emitted, never deduplicated: the stream is a delivery log,
    and a delivery log legitimately contains redeliveries. Suppression is the
    database's job, via UNIQUE(merchant_id, external_event_id).
    """
    plan.validate(len(events))

    order = plan.reorder
    if order is None:
        order = tuple(i for i in range(len(events)) if i not in plan.drops)

    # Build (received_at, event, delivery_attempt) triples, then sort by arrival.
    pending: list[tuple[SyntheticEvent, int, int, int]] = []
    for position, index in enumerate(order):
        base = events[index]
        extra_delay = plan.delays.get(index, 0)
        delivered = _delayed(base, extra_delay) if extra_delay else base

        pending.append((delivered, 1, extra_delay, position))

        # A redelivery arrives later than the original, by a deterministic gap
        # derived from its position — no randomness needed, and reproducible.
        for attempt in range(2, plan.duplicates.get(index, 0) + 2):
            redelivery_gap = extra_delay + attempt * 30
            pending.append((_delayed(base, redelivery_gap), attempt, redelivery_gap, position))

    if plan.reorder is None:
        pending.sort(key=lambda item: (item[0].received_at, item[3], item[1]))
    else:
        # An explicit reorder is the author's stated arrival order; honour it
        # rather than re-sorting by received_at.
        pending.sort(key=lambda item: (item[3], item[1]))

    deliveries: list[EventDelivery] = []
    latest_occurred_at = None

    for sequence, (event, attempt, delay_seconds, _position) in enumerate(pending, start=1):
        is_out_of_order = latest_occurred_at is not None and event.occurred_at < latest_occurred_at
        if latest_occurred_at is None or event.occurred_at > latest_occurred_at:
            latest_occurred_at = event.occurred_at

        deliveries.append(
            EventDelivery(
                envelope=DeliveryEnvelope(
                    sequence=sequence,
                    delivery_attempt=attempt,
                    is_duplicate=attempt > 1,
                    is_delayed=delay_seconds > 0,
                    delay_seconds=delay_seconds,
                    is_out_of_order=is_out_of_order,
                ),
                event=event,
            )
        )

    return tuple(deliveries)
