"""Canonical timeline reconstruction.

Pure and deterministic: no database, no network, no clock. The same events in
any delivery order always produce the same timeline.

The pipeline is deliberately ordered so that delivery pathology is neutralised
before anything counts:

    deduplicate  →  sort causally  →  fold into state  →  build attempt ledger

Out-of-order arrival stops mattering at the sort. Duplicate delivery stops
mattering at the dedupe. Both facts are retained as observations and integrity
flags rather than thrown away, so a later forensic question still has an answer.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from app.models.enums import EventType, OrderStatus
from app.services.tracing.state import (
    AttemptOutcome,
    AttemptRecord,
    DeliveryObservation,
    EventLike,
    IntegrityFlags,
    MerchantTimeline,
    OrderTimeline,
    SubscriptionTimeline,
    dominant_state,
    outcome_rank,
)

#: Events that carry a payment attempt's identity and status.
_ATTEMPT_OUTCOME_BY_EVENT = {
    EventType.PAYMENT_ATTEMPTED.value: AttemptOutcome.ATTEMPTED,
    EventType.PAYMENT_AUTHORIZED.value: AttemptOutcome.AUTHORIZED,
    EventType.PAYMENT_CAPTURED.value: AttemptOutcome.CAPTURED,
    EventType.PAYMENT_FAILED.value: AttemptOutcome.FAILED,
}

#: Event types that move an order's derived state.
_STATE_BY_EVENT = {
    EventType.ORDER_CREATED.value: OrderStatus.CREATED.value,
    EventType.CHECKOUT_STARTED.value: OrderStatus.CREATED.value,
    EventType.PAYMENT_ATTEMPTED.value: OrderStatus.ATTEMPTED.value,
    EventType.PAYMENT_AUTHORIZED.value: OrderStatus.ATTEMPTED.value,
    EventType.PAYMENT_CAPTURED.value: OrderStatus.ATTEMPTED.value,
    EventType.PAYMENT_FAILED.value: OrderStatus.ATTEMPTED.value,
    EventType.CHECKOUT_ABANDONED.value: OrderStatus.ABANDONED.value,
    EventType.ORDER_PAID.value: OrderStatus.PAID.value,
    EventType.REFUND_CREATED.value: OrderStatus.REFUNDED.value,
}


def _payload_str(event: EventLike, key: str) -> str | None:
    value = event.payload.get(key)
    return value if isinstance(value, str) else None


def _payload_money(event: EventLike, key: str = "amount_minor") -> int:
    """Read an integer minor-unit amount. Never coerces a float."""
    value = event.payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _payload_int(event: EventLike, key: str, default: int) -> int:
    value = event.payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


# -- step 1: deduplication ------------------------------------------------


def deduplicate(
    events: Sequence[EventLike],
) -> tuple[tuple[EventLike, ...], tuple[DeliveryObservation, ...]]:
    """Keep the first arrival of each event; record every arrival.

    Identity is `external_event_id`, the same key the database enforces. An
    event with no external id cannot be deduplicated and is always kept.

    Returns (unique events in delivery order, all observations).
    """
    seen: set[str] = set()
    unique: list[EventLike] = []
    observations: list[DeliveryObservation] = []

    for index, event in enumerate(events):
        external_id = event.external_event_id
        suppressed = external_id is not None and external_id in seen

        if external_id is not None:
            observations.append(
                DeliveryObservation(
                    external_event_id=external_id,
                    received_at=event.received_at,
                    occurred_at=event.occurred_at,
                    delivery_index=index,
                    suppressed=suppressed,
                )
            )

        if suppressed:
            continue

        if external_id is not None:
            seen.add(external_id)
        unique.append(event)

    return tuple(unique), tuple(observations)


# -- step 2: causal ordering ----------------------------------------------


def causal_order(events: Iterable[EventLike]) -> tuple[EventLike, ...]:
    """Sort by occurrence time. The tiebreak makes the order total and stable.

    This is the only ordering detection is ever allowed to see.
    """
    return tuple(sorted(events, key=lambda e: (e.occurred_at, e.external_event_id or "")))


def delivery_order(events: Iterable[EventLike]) -> tuple[EventLike, ...]:
    """Sort by arrival time. Forensics only — never an input to detection."""
    return tuple(sorted(events, key=lambda e: (e.received_at, e.external_event_id or "")))


# -- integrity ------------------------------------------------------------


def _integrity(
    delivered: Sequence[EventLike],
    observations: Sequence[DeliveryObservation],
    inferred_gaps: int,
) -> IntegrityFlags:
    """Measure delivery quality against the order events were handed to us."""
    out_of_order = 0
    highest: datetime | None = None
    for event in delivered:
        if highest is not None and event.occurred_at < highest:
            out_of_order += 1
        if highest is None or event.occurred_at > highest:
            highest = event.occurred_at

    max_lag = 0
    for event in delivered:
        lag = int((event.received_at - event.occurred_at).total_seconds())
        max_lag = max(max_lag, lag)

    return IntegrityFlags(
        duplicate_deliveries=sum(1 for o in observations if o.suppressed),
        out_of_order_deliveries=out_of_order,
        max_delivery_lag_seconds=max_lag,
        inferred_gaps=inferred_gaps,
    )


# -- step 4: attempt ledger -----------------------------------------------


def _build_attempts(events: Sequence[EventLike]) -> tuple[tuple[AttemptRecord, ...], int]:
    """Fold payment events into one record per payment reference.

    Returns (attempts in attempt-number order, inferred gap count).

    An attempt whose `payment.attempted` event never arrived is still recorded,
    flagged `inferred`. Dropping it would make a missing event look like an
    attempt that never happened, which is exactly the wrong conclusion.
    """
    grouped: dict[str, list[tuple[EventLike, AttemptOutcome]]] = defaultdict(list)

    for event in events:
        outcome = _ATTEMPT_OUTCOME_BY_EVENT.get(event.event_type)
        if outcome is None:
            continue
        payment_ref = _payload_str(event, "payment_ref")
        if payment_ref is None:
            continue
        grouped[payment_ref].append((event, outcome))

    # Refunds are attributed to an attempt only when the provider names one.
    # The Phase 2 refund payload carries a refund_ref and an order_ref but no
    # payment_ref, so for simulated data this set is empty and the refund stays
    # an order-level fact (`has_refund`, state `refunded`). Guessing which
    # attempt a refund belongs to would be fabrication; the order-level signal
    # is what detection actually needs.
    refunded_refs = {
        ref
        for event in events
        if event.event_type == EventType.REFUND_CREATED.value
        for ref in (_payload_str(event, "payment_ref"),)
        if ref is not None
    }

    records: list[AttemptRecord] = []
    inferred_gaps = 0

    for payment_ref, entries in grouped.items():
        ordered = sorted(entries, key=lambda pair: pair[0].occurred_at)
        best_outcome = max((outcome for _, outcome in ordered), key=outcome_rank)

        if payment_ref in refunded_refs and best_outcome is AttemptOutcome.CAPTURED:
            best_outcome = AttemptOutcome.REFUNDED

        # A timeout is stored as a failed payment event carrying a timeout code.
        failure_event = next(
            (e for e, o in ordered if o is AttemptOutcome.FAILED),
            None,
        )
        failure_code = _payload_str(failure_event, "failure_code") if failure_event else None
        if best_outcome is AttemptOutcome.FAILED and failure_code == "gateway_timeout":
            best_outcome = AttemptOutcome.TIMEOUT

        has_attempted_event = any(o is AttemptOutcome.ATTEMPTED for _, o in ordered)
        if not has_attempted_event:
            inferred_gaps += 1

        first_event = ordered[0][0]
        last_event = ordered[-1][0]

        records.append(
            AttemptRecord(
                payment_ref=payment_ref,
                attempt_number=_payload_int(first_event, "attempt_number", len(records) + 1),
                outcome=best_outcome,
                payment_method=_payload_str(first_event, "method"),
                failure_code=failure_code,
                failure_reason=(
                    _payload_str(failure_event, "failure_reason") if failure_event else None
                ),
                amount_minor=_payload_money(first_event),
                currency=_payload_str(first_event, "currency"),
                first_seen_at=first_event.occurred_at,
                last_seen_at=last_event.occurred_at,
                inferred=not has_attempted_event,
            )
        )

    records.sort(key=lambda r: (r.attempt_number, r.first_seen_at))
    return tuple(records), inferred_gaps


# -- step 3+: order reconstruction ----------------------------------------


def reconstruct_order(order_id: uuid.UUID, events: Sequence[EventLike]) -> OrderTimeline:
    """Rebuild one order's canonical timeline from its deliveries."""
    unique, observations = deduplicate(events)
    ordered = causal_order(unique)

    if not ordered:
        raise ValueError(f"cannot reconstruct order {order_id} from zero events")

    present = {event.event_type for event in ordered}
    states = [
        _STATE_BY_EVENT[event.event_type]
        for event in ordered
        if event.event_type in _STATE_BY_EVENT
    ]

    attempts, inferred_gaps = _build_attempts(ordered)

    amount_minor = 0
    currency: str | None = None
    for event in ordered:
        amount = _payload_money(event)
        if amount > amount_minor:
            amount_minor = amount
        if currency is None:
            currency = _payload_str(event, "currency")

    first = ordered[0]

    return OrderTimeline(
        order_id=order_id,
        merchant_id=first.merchant_id,
        customer_id=next((e.customer_id for e in ordered if e.customer_id), None),
        order_ref=next((_payload_str(e, "order_ref") for e in ordered), None),
        amount_minor=amount_minor,
        currency=currency,
        state=dominant_state(states),
        has_capture=EventType.PAYMENT_CAPTURED.value in present,
        has_order_paid=EventType.ORDER_PAID.value in present,
        has_refund=EventType.REFUND_CREATED.value in present,
        has_checkout_started=EventType.CHECKOUT_STARTED.value in present,
        has_checkout_abandoned=EventType.CHECKOUT_ABANDONED.value in present,
        has_recovery_action=EventType.RECOVERY_ACTION_EXECUTED.value in present,
        has_recovery_succeeded=EventType.RECOVERY_SUCCEEDED.value in present,
        has_recovery_failed=EventType.RECOVERY_FAILED.value in present,
        events=ordered,
        attempts=attempts,
        observations=observations,
        integrity=_integrity(unique, observations, inferred_gaps),
    )


def reconstruct_subscription(
    subscription_ref: str, events: Sequence[EventLike]
) -> SubscriptionTimeline:
    """Rebuild one subscription's billing history."""
    unique, observations = deduplicate(events)
    ordered = causal_order(unique)

    if not ordered:
        raise ValueError(f"cannot reconstruct subscription {subscription_ref} from zero events")

    charged = 0
    failed = 0
    failed_amount = 0
    trailing_streak = 0

    for event in ordered:
        if event.event_type == EventType.SUBSCRIPTION_CHARGED.value:
            charged += 1
            trailing_streak = 0
        elif event.event_type == EventType.SUBSCRIPTION_PAYMENT_FAILED.value:
            failed += 1
            failed_amount += _payload_money(event)
            trailing_streak += 1

    first = ordered[0]

    return SubscriptionTimeline(
        subscription_ref=subscription_ref,
        merchant_id=first.merchant_id,
        customer_id=next((e.customer_id for e in ordered if e.customer_id), None),
        currency=next((_payload_str(e, "currency") for e in ordered), None),
        charged_cycles=charged,
        failed_cycles=failed,
        failed_amount_minor=failed_amount,
        is_halted=any(e.event_type == EventType.SUBSCRIPTION_HALTED.value for e in ordered),
        trailing_failure_streak=trailing_streak,
        events=ordered,
        observations=observations,
        integrity=_integrity(unique, observations, 0),
    )


def reconstruct_merchant(merchant_id: uuid.UUID, events: Sequence[EventLike]) -> MerchantTimeline:
    """Rebuild every order and subscription timeline for one merchant."""
    by_order: dict[uuid.UUID, list[EventLike]] = defaultdict(list)
    by_subscription: dict[str, list[EventLike]] = defaultdict(list)

    for event in events:
        if event.order_id is not None:
            by_order[event.order_id].append(event)
            continue
        subscription_ref = _payload_str(event, "subscription_ref")
        if subscription_ref is not None:
            by_subscription[subscription_ref].append(event)

    orders = tuple(
        reconstruct_order(order_id, order_events)
        for order_id, order_events in sorted(by_order.items(), key=lambda kv: str(kv[0]))
    )
    subscriptions = tuple(
        reconstruct_subscription(ref, subscription_events)
        for ref, subscription_events in sorted(by_subscription.items())
    )

    unique, observations = deduplicate(events)
    inferred = sum(o.integrity.inferred_gaps for o in orders)

    return MerchantTimeline(
        merchant_id=merchant_id,
        orders=orders,
        subscriptions=subscriptions,
        integrity=_integrity(unique, observations, inferred),
    )


def timeline_view(timeline: OrderTimeline) -> list[dict[str, Any]]:
    """A forensic view pairing causal position with delivery position.

    Delivery position comes from the recorded observations — the order events
    were actually handed to reconstruction — rather than from re-sorting by
    `received_at`. Arrival order and arrival timestamp are not the same thing:
    a stream can be delivered backwards while each event keeps its original
    `received_at`, and re-sorting would erase exactly the anomaly being looked
    for.

    A caveat worth knowing when this is fed persisted rows: the `events` table
    stores arrival *time*, not arrival *order*, so delivery position then
    reflects query order and will track causal position. The signal survives in
    `received_at` either way.
    """
    first_arrival: dict[str, int] = {}
    for observation in timeline.observations:
        first_arrival.setdefault(observation.external_event_id, observation.delivery_index)

    delivery_positions = {
        external_id: rank
        for rank, (external_id, _) in enumerate(
            sorted(first_arrival.items(), key=lambda item: item[1]), start=1
        )
    }

    return [
        {
            "causal_position": index,
            "delivery_position": (
                delivery_positions.get(event.external_event_id)
                if event.external_event_id is not None
                else None
            ),
            "external_event_id": event.external_event_id,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "received_at": event.received_at,
            "delay_seconds": int((event.received_at - event.occurred_at).total_seconds()),
        }
        for index, event in enumerate(timeline.events, start=1)
    ]
