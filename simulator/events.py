"""Event and payload builders.

Payloads are provider-neutral and carry only what a real webhook would carry.
Scenario names, risk types, expected outcomes, and any other ground-truth
signal are forbidden here — a detector must not be able to read the answer out
of its own input. `tests/simulator/test_event_mapping.py` enforces this.

Money in payloads is always an integer count of minor units, under keys ending
in `_minor` so the unit is unambiguous at the wire.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.models.enums import EventType
from simulator.models import SyntheticEvent, SyntheticOrder, SyntheticPaymentAttempt
from simulator.rng import DeterministicRng

#: Keys that must never appear in a payload. Asserted in tests.
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "scenario",
        "scenario_id",
        "risk_type",
        "expected_risk",
        "expected_risks",
        "ground_truth",
        "anomaly",
        "anomaly_kind",
        "is_duplicate",
        "amount_at_risk",
        "narrative",
    }
)


class EventIdFactory:
    """Deterministic, monotonic external event identifiers.

    The counter is the idempotency key's uniqueness source. Duplicate delivery
    reuses an already-issued id rather than allocating a new one.
    """

    __slots__ = ("_counter", "_scenario_id", "_seed")

    def __init__(self, scenario_id: str, seed: int) -> None:
        self._scenario_id = scenario_id
        self._seed = seed
        self._counter = 0

    def next_id(self) -> str:
        self._counter += 1
        return f"sim_evt_{self._scenario_id}_{self._seed}_{self._counter:06d}"

    @property
    def issued(self) -> int:
        return self._counter


def build_event(
    rng: DeterministicRng,
    ids: EventIdFactory,
    *,
    merchant_id: uuid.UUID,
    event_type: EventType,
    occurred_at: datetime,
    received_at: datetime,
    payload: dict[str, object],
    customer_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
) -> SyntheticEvent:
    if received_at < occurred_at:
        raise ValueError(
            f"received_at ({received_at}) must not precede occurred_at ({occurred_at})"
        )

    leaked = FORBIDDEN_PAYLOAD_KEYS & set(payload)
    if leaked:
        raise ValueError(f"ground-truth keys must not appear in a payload: {sorted(leaked)}")

    return SyntheticEvent(
        id=rng.uuid(),
        merchant_id=merchant_id,
        customer_id=customer_id,
        order_id=order_id,
        external_event_id=ids.next_id(),
        event_type=event_type.value,
        payload=payload,
        occurred_at=occurred_at,
        received_at=received_at,
    )


# -- payload builders -----------------------------------------------------


def order_payload(order: SyntheticOrder) -> dict[str, object]:
    return {
        "order_ref": order.external_order_id,
        "amount_minor": order.amount,
        "currency": order.currency,
        "status": order.status,
    }


def attempt_payload(attempt: SyntheticPaymentAttempt, order: SyntheticOrder) -> dict[str, object]:
    payload: dict[str, object] = {
        "order_ref": order.external_order_id,
        "payment_ref": attempt.external_payment_id,
        "amount_minor": attempt.amount,
        "currency": attempt.currency,
        "method": attempt.payment_method,
        "provider": attempt.provider,
        "attempt_number": attempt.attempt_number,
        "status": attempt.status,
    }
    if attempt.failure_code is not None:
        payload["failure_code"] = attempt.failure_code
        payload["failure_reason"] = attempt.failure_reason
    return payload


def checkout_payload(order: SyntheticOrder, *, session_ref: str) -> dict[str, object]:
    return {
        "order_ref": order.external_order_id,
        "session_ref": session_ref,
        "amount_minor": order.amount,
        "currency": order.currency,
    }


def subscription_payload(
    *,
    subscription_ref: str,
    amount_minor: int,
    currency: str,
    cycle: int,
    failure_code: str | None = None,
    failure_reason: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "subscription_ref": subscription_ref,
        "amount_minor": amount_minor,
        "currency": currency,
        "cycle": cycle,
    }
    if failure_code is not None:
        payload["failure_code"] = failure_code
        payload["failure_reason"] = failure_reason
    return payload


def refund_payload(
    order: SyntheticOrder, *, refund_ref: str, amount_minor: int
) -> dict[str, object]:
    if amount_minor > order.amount:
        raise ValueError(f"refund {amount_minor} exceeds captured amount {order.amount}")
    return {
        "order_ref": order.external_order_id,
        "refund_ref": refund_ref,
        "amount_minor": amount_minor,
        "currency": order.currency,
    }


def recovery_payload(
    order: SyntheticOrder, *, action_ref: str, action_type: str
) -> dict[str, object]:
    """Payload for a historical recovery.* event.

    The simulator records that a recovery action happened in this synthetic
    history. It does NOT create recovery_cases or recovery_actions rows, and it
    never asserts that an approval or policy decision took place — that
    authority belongs to the policy engine (ADR 0005).
    """
    return {
        "order_ref": order.external_order_id,
        "action_ref": action_ref,
        "action_type": action_type,
        "amount_minor": order.amount,
        "currency": order.currency,
    }
