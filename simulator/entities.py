"""Synthetic entity builders.

All identifiers are derived from the seeded RNG, so a run is reproducible.
All money is integer minor units drawn with `randrange` over integer bounds —
no float ever enters the chain.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.models.enums import OrderStatus, PaymentMethod, PaymentStatus
from simulator.config import (
    DEFAULT_CURRENCY,
    TYPICAL_LIFETIME_VALUE_PAISE,
    TYPICAL_ORDER_PAISE,
)
from simulator.models import (
    SyntheticCustomer,
    SyntheticMerchant,
    SyntheticOrder,
    SyntheticPaymentAttempt,
)
from simulator.rng import DeterministicRng

#: Provider name recorded on generated attempts. Never "razorpay".
SIMULATOR_PROVIDER = "simulator"

_FIRST_NAMES = ("Asha", "Ravi", "Meera", "Vikram", "Neha", "Arjun", "Divya", "Karan")
_LAST_NAMES = ("Sharma", "Iyer", "Patel", "Nair", "Reddy", "Bose", "Khan", "Menon")


def external_ref(kind: str, seed: int, index: int) -> str:
    """Deterministic, human-readable external reference."""
    return f"sim_{kind}_{seed}_{index}"


def build_merchant(
    rng: DeterministicRng,
    *,
    seed: int,
    index: int = 1,
    currency: str = DEFAULT_CURRENCY,
) -> SyntheticMerchant:
    return SyntheticMerchant(
        id=rng.uuid(),
        external_ref=external_ref("merch", seed, index),
        name=f"Synthetic Merchant {index}",
        currency=currency,
        timezone="Asia/Kolkata",
    )


def build_customer(
    rng: DeterministicRng,
    *,
    seed: int,
    index: int,
    merchant_id: uuid.UUID,
    lifetime_value_range: tuple[int, int, int] = TYPICAL_LIFETIME_VALUE_PAISE,
    contactable: bool = True,
) -> SyntheticCustomer:
    first = rng.choice(_FIRST_NAMES)
    last = rng.choice(_LAST_NAMES)
    low, high, step = lifetime_value_range

    return SyntheticCustomer(
        id=rng.uuid(),
        merchant_id=merchant_id,
        external_customer_id=external_ref("cust", seed, index),
        name=f"{first} {last}",
        email=f"{first.lower()}.{last.lower()}{index}@example.invalid",
        phone=f"+9198{rng.randrange(10_000_000, 99_999_999):08d}",
        lifetime_value=rng.randrange(low, high, step),
        contactable=contactable,
        contact_count=0,
    )


def build_order(
    rng: DeterministicRng,
    *,
    seed: int,
    index: int,
    merchant_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    amount_range: tuple[int, int, int] = TYPICAL_ORDER_PAISE,
    amount: int | None = None,
    currency: str = DEFAULT_CURRENCY,
    status: str = OrderStatus.CREATED.value,
) -> SyntheticOrder:
    if amount is None:
        low, high, step = amount_range
        amount = rng.randrange(low, high, step)

    if not isinstance(amount, int) or isinstance(amount, bool):
        raise TypeError("order amount must be an integer count of minor units")

    return SyntheticOrder(
        id=rng.uuid(),
        merchant_id=merchant_id,
        customer_id=customer_id,
        external_order_id=external_ref("order", seed, index),
        amount=amount,
        currency=currency,
        status=status,
    )


def build_payment_attempt(
    rng: DeterministicRng,
    *,
    seed: int,
    index: int,
    order: SyntheticOrder,
    attempt_number: int,
    status: str,
    attempted_at: datetime,
    payment_method: str = PaymentMethod.CARD.value,
    failure_code: str | None = None,
    failure_reason: str | None = None,
    amount: int | None = None,
) -> SyntheticPaymentAttempt:
    if attempt_number < 1:
        raise ValueError(f"attempt_number must be >= 1, got {attempt_number}")

    resolved_amount = order.amount if amount is None else amount
    if not isinstance(resolved_amount, int) or isinstance(resolved_amount, bool):
        raise TypeError("payment amount must be an integer count of minor units")

    if status == PaymentStatus.FAILED.value and failure_code is None:
        raise ValueError("a failed attempt must carry a failure_code")

    return SyntheticPaymentAttempt(
        id=rng.uuid(),
        order_id=order.id,
        customer_id=order.customer_id,
        external_payment_id=external_ref("pay", seed, index),
        amount=resolved_amount,
        currency=order.currency,
        payment_method=payment_method,
        provider=SIMULATOR_PROVIDER,
        status=status,
        failure_code=failure_code,
        failure_reason=failure_reason,
        attempt_number=attempt_number,
        attempted_at=attempted_at,
    )


def with_status(order: SyntheticOrder, status: str) -> SyntheticOrder:
    """Return a copy of an order at a new status. Never mutates."""
    return SyntheticOrder(
        id=order.id,
        merchant_id=order.merchant_id,
        customer_id=order.customer_id,
        external_order_id=order.external_order_id,
        amount=order.amount,
        currency=order.currency,
        status=status,
    )
