"""Webhook pathology emitters for the v2 benchmark.

The v1 `delivery.py` already corrupts *arrival* — duplicating, delaying,
reordering, dropping — without ever touching `occurred_at`. It is reused
verbatim here rather than reimplemented, because it is covered by the existing
suite and the separation it enforces is exactly the one this needs.

What v1 has no notion of is a **malformed** payload: a delivery that is not
merely late or repeated but structurally invalid. Those are added here.

The distinction matters for what each proves:

* duplicate / delayed / out-of-order / dropped — the pipeline must reach the
  *same conclusion* despite them;
* malformed — the pipeline must **reject** them, loudly, without poisoning the
  timeline around them.

A malformed event is deliberately kept in its own type. Returning a corrupted
`SyntheticEvent` would mean every consumer had to remember to check a flag;
returning a `MalformedDelivery` means a consumer that ignores them cannot
accidentally ingest one.

Rates follow the revised plan: ~5% duplicates, delays up to 30 minutes, ~3%
out-of-order, ~1% malformed. All integer basis points, all drawn from a seeded
sub-stream.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from simulator.delivery import DeliveryPlan
from simulator.models import SyntheticEvent
from simulator.rng import DeterministicRng

#: Basis points, matching the revised plan's stated rates.
DEFAULT_DUPLICATE_RATE_BPS = 500
DEFAULT_DELAY_RATE_BPS = 1_500
DEFAULT_OUT_OF_ORDER_RATE_BPS = 300
DEFAULT_MALFORMED_RATE_BPS = 100

#: Delayed deliveries arrive up to thirty minutes late.
MAX_DELAY_SECONDS = 30 * 60

BPS_SCALE = 10_000


class Corruption(StrEnum):
    """How a payload was broken.

    Each maps to a distinct rejection the ingestion schema already performs, so
    a malformed delivery exercises a real guard rather than a hypothetical one.
    """

    #: A required money field is absent.
    MISSING_AMOUNT = "missing_amount"
    #: Money arrives as a string. Ingestion requires StrictInt.
    AMOUNT_AS_STRING = "amount_as_string"
    #: Money arrives as a float. ADR 0001 forbids it end to end.
    AMOUNT_AS_FLOAT = "amount_as_float"
    #: A negative amount, which no non-refund event may carry.
    NEGATIVE_AMOUNT = "negative_amount"
    #: The event identity is blank, so it cannot be deduplicated.
    BLANK_EXTERNAL_ID = "blank_external_id"
    #: An event type outside the known vocabulary.
    UNKNOWN_EVENT_TYPE = "unknown_event_type"


CORRUPTIONS: tuple[Corruption, ...] = tuple(Corruption)


@dataclass(frozen=True, slots=True)
class MalformedDelivery:
    """A delivery that must be rejected rather than reconstructed.

    Carries the original `external_event_id` so a test can assert the *valid*
    events around it were unaffected — a malformed payload must not poison the
    timeline it arrived in.
    """

    corruption: Corruption
    payload: Mapping[str, object]
    external_event_id: str
    event_type: str


def _fires(rng: DeterministicRng, rate_bps: int) -> bool:
    if rate_bps <= 0:
        return False
    if rate_bps >= BPS_SCALE:
        return True
    return rng.randint(1, BPS_SCALE) <= rate_bps


def build_delivery_plan(
    rng: DeterministicRng,
    event_count: int,
    *,
    duplicate_rate_bps: int = DEFAULT_DUPLICATE_RATE_BPS,
    delay_rate_bps: int = DEFAULT_DELAY_RATE_BPS,
    out_of_order_rate_bps: int = DEFAULT_OUT_OF_ORDER_RATE_BPS,
) -> DeliveryPlan:
    """A randomised but reproducible delivery plan for a benchmark run.

    Out-of-order is produced by swapping adjacent pairs rather than by shuffling
    wholesale: real webhook reordering is local, and a full shuffle would be a
    much easier problem to solve than the one production presents.
    """
    if event_count < 0:
        raise ValueError(f"event_count must be non-negative, got {event_count}")
    if event_count == 0:
        return DeliveryPlan()

    duplicate_rng = rng.derive("duplicates")
    delay_rng = rng.derive("delays")
    reorder_rng = rng.derive("reorder")

    duplicates = {
        index: 1 for index in range(event_count) if _fires(duplicate_rng, duplicate_rate_bps)
    }
    delays = {
        index: delay_rng.randint(1, MAX_DELAY_SECONDS)
        for index in range(event_count)
        if _fires(delay_rng, delay_rate_bps)
    }

    order = list(range(event_count))
    index = 0
    while index < event_count - 1:
        if _fires(reorder_rng, out_of_order_rate_bps):
            order[index], order[index + 1] = order[index + 1], order[index]
            # Skip past the pair just swapped. Without this, consecutive swaps
            # chain and an event can drift several positions, which is a
            # different (and easier) pathology than the adjacent transposition
            # a real webhook queue produces.
            index += 2
            continue
        index += 1

    reorder = tuple(order) if order != list(range(event_count)) else None

    return DeliveryPlan(duplicates=duplicates, delays=delays, reorder=reorder)


def _corrupt(payload: Mapping[str, object], corruption: Corruption) -> dict[str, object]:
    broken = dict(payload)

    if corruption is Corruption.MISSING_AMOUNT:
        broken.pop("amount_minor", None)
    elif corruption is Corruption.AMOUNT_AS_STRING:
        broken["amount_minor"] = str(broken.get("amount_minor", 0))
    elif corruption is Corruption.AMOUNT_AS_FLOAT:
        broken["amount_minor"] = float(broken.get("amount_minor", 0) or 0)
    elif corruption is Corruption.NEGATIVE_AMOUNT:
        broken["amount_minor"] = -abs(int(broken.get("amount_minor", 1) or 1))

    return broken


def malformed_from(event: SyntheticEvent, corruption: Corruption) -> MalformedDelivery:
    """Break one event in a specific, named way."""
    external_id = "" if corruption is Corruption.BLANK_EXTERNAL_ID else event.external_event_id
    event_type = (
        "payment.exploded" if corruption is Corruption.UNKNOWN_EVENT_TYPE else event.event_type
    )

    return MalformedDelivery(
        corruption=corruption,
        payload=_corrupt(event.payload, corruption),
        external_event_id=external_id,
        event_type=event_type,
    )


def emit_malformed(
    rng: DeterministicRng,
    events: Sequence[SyntheticEvent],
    *,
    rate_bps: int = DEFAULT_MALFORMED_RATE_BPS,
) -> tuple[MalformedDelivery, ...]:
    """Draw a malformed variant for roughly `rate_bps` of the events.

    Returned *alongside* the valid stream, never substituted into it: the point
    is that a rejected delivery leaves the surrounding timeline intact, and that
    can only be asserted if the valid events are still all present.
    """
    malformed_rng = rng.derive("malformed")
    kind_rng = rng.derive("malformed.kind")

    return tuple(
        malformed_from(event, kind_rng.choice(CORRUPTIONS))
        for event in events
        if _fires(malformed_rng, rate_bps)
    )


__all__ = [
    "CORRUPTIONS",
    "DEFAULT_DUPLICATE_RATE_BPS",
    "DEFAULT_MALFORMED_RATE_BPS",
    "DEFAULT_OUT_OF_ORDER_RATE_BPS",
    "MAX_DELAY_SECONDS",
    "Corruption",
    "MalformedDelivery",
    "build_delivery_plan",
    "emit_malformed",
    "malformed_from",
]
