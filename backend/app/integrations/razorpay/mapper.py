"""Razorpay's event names to RevTrace's provider-neutral vocabulary.

`EventType`'s own docstring names this module: *"Razorpay webhook names are
translated into these by `integrations/razorpay/mapper.py` (Phase 8), so the
leak graph never depends on one provider's naming."* This is that translation,
and it is the only place a Razorpay event name appears outside a test.

**An unknown event is refused, never guessed.** The table below is exhaustive
for what RevTrace understands, and anything absent from it raises
`UnmappedEvent`. The alternative — mapping the unrecognised to something
plausible — would put a provider's new event into the leak graph under a
meaning nobody chose, and a timeline is evidence. Refusing loudly is the same
rule the falsification layer, the loader and the gate already follow.

Refusal is not failure, though: the route acknowledges an unmapped event with
200 so Razorpay stops redelivering it, and records that it arrived. Silence
would invite retries; a 4xx would claim the sender did something wrong.
"""

from __future__ import annotations

from app.models.enums import EventType

#: Razorpay webhook event name -> RevTrace event type.
#:
#: Deliberately partial. Razorpay emits far more than this, and each entry here
#: is one RevTrace has a use for — a detection rule, a timeline step or a
#: recovery outcome. Adding a row is a decision about the leak graph, not
#: bookkeeping, which is why the table is written out rather than derived.
EVENT_MAP: dict[str, EventType] = {
    # Payments
    "payment.authorized": EventType.PAYMENT_AUTHORIZED,
    "payment.captured": EventType.PAYMENT_CAPTURED,
    "payment.failed": EventType.PAYMENT_FAILED,
    # Orders
    "order.paid": EventType.ORDER_PAID,
    # Refunds
    "refund.created": EventType.REFUND_CREATED,
    # Subscriptions
    "subscription.charged": EventType.SUBSCRIPTION_CHARGED,
    "subscription.halted": EventType.SUBSCRIPTION_HALTED,
    # Payment links. Razorpay reports a link's completion as `payment_link.paid`;
    # to the leak graph that is an order being paid, which is what the detection
    # and recovery layers already reason about.
    "payment_link.paid": EventType.ORDER_PAID,
}

#: Events RevTrace recognises but deliberately does not record.
#:
#: Distinguished from "unknown" on purpose: these are expected traffic, and
#: treating them as unmapped would make a normal delivery look like a gap in the
#: table. They carry no information the leak graph uses — a link being created
#: is something RevTrace already knows, having created it.
IGNORED_EVENTS: frozenset[str] = frozenset(
    {
        "payment_link.created",
        "payment_link.cancelled",
        "payment_link.expired",
        "payment_link.partially_paid",
    }
)


class UnmappedEvent(LookupError):
    """A Razorpay event RevTrace has no meaning for.

    Carries the name so an operator can decide whether to add a row, and
    nothing else from the payload.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"razorpay event {name!r} has no RevTrace event type. It is neither "
            f"mapped nor explicitly ignored, so it is refused rather than "
            f"recorded under a meaning nobody chose."
        )
        self.name = name


def is_ignored(name: str) -> bool:
    """Whether this event is known and deliberately not recorded."""
    return name in IGNORED_EVENTS


def map_event(name: str) -> EventType:
    """Translate one Razorpay event name. Raises on anything unrecognised."""
    try:
        return EVENT_MAP[name]
    except KeyError:
        raise UnmappedEvent(name) from None


def supported_events() -> frozenset[str]:
    """Every Razorpay event name this mapper accepts or knowingly ignores."""
    return frozenset(EVENT_MAP) | IGNORED_EVENTS


__all__ = [
    "EVENT_MAP",
    "IGNORED_EVENTS",
    "UnmappedEvent",
    "is_ignored",
    "map_event",
    "supported_events",
]
