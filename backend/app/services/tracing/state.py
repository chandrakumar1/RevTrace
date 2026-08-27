"""Reconstructed state value objects.

Everything here is a frozen dataclass with no I/O and no database dependency.
Reconstruction consumes anything satisfying `EventLike` — a persisted ORM row, a
simulator event, or a plain test double — so the logic can be exercised without
a connection.

Four concepts are kept strictly apart and never conflated:

* **occurrence time** (`occurred_at`) — causal truth, the only input to detection
* **delivery time** (`received_at`) — forensics only
* **causal ordering** — sorted by `(occurred_at, external_event_id)`
* **delivery ordering** — the order events were handed to us

Nothing is discarded. Suppressed duplicate deliveries survive as observations so
"this arrived three times" stays answerable after the fact.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.models.enums import OrderStatus


@runtime_checkable
class EventLike(Protocol):
    """The structural shape reconstruction needs from an event."""

    external_event_id: str | None
    event_type: str
    occurred_at: datetime
    received_at: datetime
    #: dict rather than Mapping: a Protocol's mutable attributes are
    #: invariant, and the ORM column is typed dict.
    payload: dict[str, Any]
    merchant_id: uuid.UUID
    customer_id: uuid.UUID | None
    order_id: uuid.UUID | None


class AttemptOutcome(StrEnum):
    """Final outcome of one payment attempt, derived from its events."""

    ATTEMPTED = "attempted"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    TIMEOUT = "timeout"
    REFUNDED = "refunded"


#: Which outcome wins when an attempt has several status-bearing events.
#: Later stages supersede earlier ones; refunded supersedes everything.
_OUTCOME_RANK = {
    AttemptOutcome.ATTEMPTED: 0,
    AttemptOutcome.FAILED: 1,
    AttemptOutcome.TIMEOUT: 1,
    AttemptOutcome.AUTHORIZED: 2,
    AttemptOutcome.CAPTURED: 3,
    AttemptOutcome.REFUNDED: 4,
}


def outcome_rank(outcome: AttemptOutcome) -> int:
    return _OUTCOME_RANK[outcome]


@dataclass(frozen=True, slots=True)
class DeliveryObservation:
    """One arrival of one event, including arrivals that were suppressed.

    Kept so that duplicate delivery remains visible after deduplication.
    """

    external_event_id: str
    received_at: datetime
    occurred_at: datetime
    #: Position in the delivery sequence handed to reconstruction, 0-based.
    delivery_index: int
    #: True when an earlier arrival of this same event was already seen.
    suppressed: bool


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One payment attempt, folded from its events.

    `inferred` marks an attempt whose `payment.attempted` event never arrived
    but whose existence is implied by a later event for the same payment
    reference. Detection counts inferred attempts — that is what lets the
    missing-event scenario still reach the right conclusion — and the flag keeps
    the inference visible rather than silent.
    """

    payment_ref: str
    attempt_number: int
    outcome: AttemptOutcome
    payment_method: str | None
    failure_code: str | None
    failure_reason: str | None
    #: Integer minor units.
    amount_minor: int
    currency: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    inferred: bool = False

    @property
    def is_successful(self) -> bool:
        return self.outcome in {AttemptOutcome.CAPTURED, AttemptOutcome.REFUNDED}

    @property
    def is_failure(self) -> bool:
        return self.outcome in {AttemptOutcome.FAILED, AttemptOutcome.TIMEOUT}


@dataclass(frozen=True, slots=True)
class IntegrityFlags:
    """Delivery-quality facts about a reconstructed timeline.

    `duplicate_deliveries` is 0 for a timeline rebuilt from persisted rows: the
    Phase 1 unique constraint rejected the redeliveries at ingestion, so they
    never became rows. It is non-zero only when reconstruction is fed a raw
    delivery stream directly. That is a property of where the data came from,
    not a defect.
    """

    duplicate_deliveries: int = 0
    out_of_order_deliveries: int = 0
    max_delivery_lag_seconds: int = 0
    inferred_gaps: int = 0

    @property
    def is_clean(self) -> bool:
        return (
            self.duplicate_deliveries == 0
            and self.out_of_order_deliveries == 0
            and self.inferred_gaps == 0
        )


@dataclass(frozen=True, slots=True)
class OrderTimeline:
    """One order's reconstructed history.

    `state` is derived from events, never read from `orders.status`. The three
    booleans are deliberately separate from it: an order can carry a captured
    payment without ever reaching `paid`, which is precisely the reconciliation
    anomaly, and collapsing them into one status would hide it.
    """

    order_id: uuid.UUID
    merchant_id: uuid.UUID
    customer_id: uuid.UUID | None
    order_ref: str | None
    #: Integer minor units. Taken from the order's own events.
    amount_minor: int
    currency: str | None
    state: str
    has_capture: bool
    has_order_paid: bool
    has_refund: bool
    has_checkout_started: bool
    has_checkout_abandoned: bool
    has_recovery_action: bool
    has_recovery_succeeded: bool
    has_recovery_failed: bool
    events: tuple[EventLike, ...] = ()
    attempts: tuple[AttemptRecord, ...] = ()
    observations: tuple[DeliveryObservation, ...] = ()
    integrity: IntegrityFlags = field(default_factory=IntegrityFlags)

    @property
    def first_occurred_at(self) -> datetime | None:
        return self.events[0].occurred_at if self.events else None

    @property
    def last_occurred_at(self) -> datetime | None:
        return self.events[-1].occurred_at if self.events else None

    @property
    def failed_attempts(self) -> tuple[AttemptRecord, ...]:
        return tuple(a for a in self.attempts if a.is_failure)

    @property
    def successful_attempts(self) -> tuple[AttemptRecord, ...]:
        return tuple(a for a in self.attempts if a.is_successful)

    @property
    def reached_terminal_success(self) -> bool:
        """Did this order ever actually get paid?

        The single most important suppression signal in detection: if this is
        true, no repeated-failure risk may be reported no matter how many
        failures preceded it.
        """
        return self.has_order_paid or self.has_capture

    @property
    def captured_amount_minor(self) -> int:
        """Money that actually arrived, in integer minor units."""
        return sum(a.amount_minor for a in self.attempts if a.is_successful)


@dataclass(frozen=True, slots=True)
class SubscriptionTimeline:
    """One subscription's billing history.

    Subscription events carry no `order_id` in the Phase 1 schema, so they are
    grouped by the `subscription_ref` in their payload instead.
    """

    subscription_ref: str
    merchant_id: uuid.UUID
    customer_id: uuid.UUID | None
    currency: str | None
    charged_cycles: int
    failed_cycles: int
    #: Integer minor units, summed over failed cycles only.
    failed_amount_minor: int
    is_halted: bool
    #: Consecutive failures at the end of the history, with no later success.
    trailing_failure_streak: int
    events: tuple[EventLike, ...] = ()
    observations: tuple[DeliveryObservation, ...] = ()
    integrity: IntegrityFlags = field(default_factory=IntegrityFlags)

    @property
    def last_occurred_at(self) -> datetime | None:
        return self.events[-1].occurred_at if self.events else None


@dataclass(frozen=True, slots=True)
class MerchantTimeline:
    """Everything reconstructed for one merchant."""

    merchant_id: uuid.UUID
    orders: tuple[OrderTimeline, ...] = ()
    subscriptions: tuple[SubscriptionTimeline, ...] = ()
    integrity: IntegrityFlags = field(default_factory=IntegrityFlags)

    def order(self, order_id: uuid.UUID) -> OrderTimeline | None:
        return next((o for o in self.orders if o.order_id == order_id), None)


#: Terminal-state precedence. A later refund supersedes paid; paid supersedes
#: abandonment; any attempt supersedes a bare created.
STATE_RANK: Mapping[str, int] = {
    OrderStatus.CREATED.value: 0,
    OrderStatus.ATTEMPTED.value: 1,
    OrderStatus.ABANDONED.value: 2,
    OrderStatus.CANCELLED.value: 2,
    OrderStatus.PAID.value: 3,
    OrderStatus.REFUNDED.value: 4,
}


def dominant_state(states: Sequence[str]) -> str:
    """The winning state among those observed. Terminal states are sticky."""
    if not states:
        return OrderStatus.CREATED.value
    return max(states, key=lambda s: STATE_RANK.get(s, 0))
