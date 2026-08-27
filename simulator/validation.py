"""Input validation and output invariants.

Invalid scenarios and parameters are rejected loudly rather than producing
quietly wrong data.
"""

from __future__ import annotations

from app.models.enums import EventType, OrderStatus, PaymentMethod, PaymentStatus, RiskType
from simulator.clock import is_utc
from simulator.models import EntitySet, EventDelivery, GroundTruth

#: Maximum length of the external_event_id column in the Phase 1 schema.
MAX_EXTERNAL_ID_LENGTH = 128


class SimulationError(Exception):
    """Base class for simulator errors."""


class UnknownScenarioError(SimulationError):
    """The requested scenario is not in the registry."""


class InvalidSeedError(SimulationError):
    """The seed is not a usable non-negative integer."""


class InvariantViolation(SimulationError):
    """Generated output violates a guarantee the simulator makes."""


def validate_seed(seed: object) -> int:
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise InvalidSeedError(f"seed must be an int, got {type(seed).__name__}")
    if seed < 0:
        raise InvalidSeedError(f"seed must be non-negative, got {seed}")
    return seed


def validate_entities(entities: EntitySet) -> None:
    """Every entity must satisfy the Phase 1 schema and enum vocabularies."""
    merchant_ids = {m.id for m in entities.merchants}
    customer_ids = {c.id for c in entities.customers}
    order_ids = {o.id for o in entities.orders}

    for customer in entities.customers:
        if customer.merchant_id not in merchant_ids:
            raise InvariantViolation(f"customer {customer.external_customer_id} has no merchant")
        if customer.lifetime_value < 0:
            raise InvariantViolation("lifetime_value must be non-negative")
        if not isinstance(customer.lifetime_value, int):
            raise InvariantViolation("lifetime_value must be an integer minor-unit amount")

    for order in entities.orders:
        if order.merchant_id not in merchant_ids:
            raise InvariantViolation(f"order {order.external_order_id} has no merchant")
        if order.customer_id is not None and order.customer_id not in customer_ids:
            raise InvariantViolation(
                f"order {order.external_order_id} references a missing customer"
            )
        if order.status not in OrderStatus.values():
            raise InvariantViolation(f"invalid order status {order.status!r}")
        if not isinstance(order.amount, int) or order.amount < 0:
            raise InvariantViolation("order amount must be a non-negative integer")
        if len(order.currency) != 3:
            raise InvariantViolation(f"invalid currency {order.currency!r}")

    by_order: dict[object, list[int]] = {}
    for attempt in entities.payment_attempts:
        if attempt.order_id not in order_ids:
            raise InvariantViolation(f"attempt {attempt.external_payment_id} has no order")
        if attempt.status not in PaymentStatus.values():
            raise InvariantViolation(f"invalid payment status {attempt.status!r}")
        if attempt.payment_method not in PaymentMethod.values():
            raise InvariantViolation(f"invalid payment method {attempt.payment_method!r}")
        if attempt.attempt_number < 1:
            raise InvariantViolation("attempt_number must be >= 1")
        if not isinstance(attempt.amount, int) or attempt.amount < 0:
            raise InvariantViolation("attempt amount must be a non-negative integer")
        if not is_utc(attempt.attempted_at):
            raise InvariantViolation("attempted_at must be timezone-aware UTC")
        by_order.setdefault(attempt.order_id, []).append(attempt.attempt_number)

    for order_id, numbers in by_order.items():
        expected = list(range(1, len(numbers) + 1))
        if sorted(numbers) != expected:
            raise InvariantViolation(
                f"attempt numbers for order {order_id} are not contiguous from 1: {sorted(numbers)}"
            )


def validate_deliveries(deliveries: tuple[EventDelivery, ...]) -> None:
    """Every delivered event must satisfy the Phase 1 `events` contract."""
    for delivery in deliveries:
        event = delivery.event

        if event.event_type not in EventType.values():
            raise InvariantViolation(f"invalid event_type {event.event_type!r}")
        if not is_utc(event.occurred_at) or not is_utc(event.received_at):
            raise InvariantViolation("event timestamps must be timezone-aware UTC")
        if event.received_at < event.occurred_at:
            raise InvariantViolation(
                f"received_at precedes occurred_at for {event.external_event_id}"
            )
        if len(event.external_event_id) > MAX_EXTERNAL_ID_LENGTH:
            raise InvariantViolation(
                f"external_event_id exceeds {MAX_EXTERNAL_ID_LENGTH} characters"
            )
        if not isinstance(event.payload, dict):
            raise InvariantViolation("payload must be a dict")

        for key, value in event.payload.items():
            if key.endswith("_minor") and (not isinstance(value, int) or isinstance(value, bool)):
                raise InvariantViolation(
                    f"payload key {key!r} must hold an integer minor-unit amount"
                )


def validate_ground_truth(ground_truth: GroundTruth) -> None:
    for risk in ground_truth.expected_risks:
        if risk.risk_type not in RiskType.values():
            raise InvariantViolation(f"invalid risk_type {risk.risk_type!r}")
        if not isinstance(risk.amount_at_risk, int) or risk.amount_at_risk < 0:
            raise InvariantViolation("amount_at_risk must be a non-negative integer")

    if ground_truth.expected_persisted_event_count > ground_truth.emitted_event_count:
        raise InvariantViolation("expected_persisted_event_count cannot exceed emitted_event_count")
