"""Simulation constants and parameters.

Every monetary constant is an integer count of minor units (paise for INR).
No float appears anywhere in this module, by construction (ADR 0001).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.models.mixins import DEFAULT_CURRENCY

__all__ = [
    "DEFAULT_CURRENCY",
    "FAILURE_REASONS",
    "HIGH_VALUE_ORDER_PAISE",
    "ScenarioCategory",
    "ScenarioParams",
    "TYPICAL_ORDER_PAISE",
    "FailureCode",
]

# -- money ranges, integer minor units ------------------------------------

#: Typical order value: 499.00 to 4999.00 INR, in whole-rupee steps.
TYPICAL_ORDER_PAISE: tuple[int, int, int] = (49_900, 499_900, 100)

#: High-value order: 24999.00 to 99999.00 INR.
HIGH_VALUE_ORDER_PAISE: tuple[int, int, int] = (2_499_900, 9_999_900, 100)

#: Typical recurring subscription charge: 299.00 to 1999.00 INR.
SUBSCRIPTION_CHARGE_PAISE: tuple[int, int, int] = (29_900, 199_900, 100)

#: Customer lifetime value range.
TYPICAL_LIFETIME_VALUE_PAISE: tuple[int, int, int] = (0, 5_000_000, 100)
HIGH_LIFETIME_VALUE_PAISE: tuple[int, int, int] = (5_000_000, 50_000_000, 100)


# -- failure vocabulary ---------------------------------------------------


class FailureCode(StrEnum):
    """Provider-neutral failure codes.

    Deliberately not Razorpay's codes: translation from a provider's vocabulary
    into ours belongs in integrations/razorpay/mapper.py (Phase 8).
    """

    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_DECLINED = "card_declined"
    GATEWAY_TIMEOUT = "gateway_timeout"
    BANK_UNAVAILABLE = "bank_unavailable"
    AUTHENTICATION_FAILED = "authentication_failed"


FAILURE_REASONS: dict[str, str] = {
    FailureCode.INSUFFICIENT_FUNDS.value: "Insufficient funds in the customer account",
    FailureCode.CARD_DECLINED.value: "Card declined by the issuing bank",
    FailureCode.GATEWAY_TIMEOUT.value: "Payment gateway did not respond in time",
    FailureCode.BANK_UNAVAILABLE.value: "Issuing bank temporarily unavailable",
    FailureCode.AUTHENTICATION_FAILED.value: "Customer authentication was not completed",
}


# -- timing ---------------------------------------------------------------

#: Normal delivery lag range in seconds, inclusive.
NORMAL_DELIVERY_LAG_SECONDS: tuple[int, int] = (0, 5)

#: Gap between successive payment attempts on one order.
RETRY_GAP_SECONDS: tuple[int, int] = (60, 300)

#: Delay applied in the delayed-delivery scenario: six hours.
LONG_DELIVERY_DELAY_SECONDS = 6 * 60 * 60

#: Interval between subscription billing cycles: thirty days.
SUBSCRIPTION_CYCLE_SECONDS = 30 * 24 * 60 * 60


# -- scenario metadata ----------------------------------------------------


class ScenarioCategory(StrEnum):
    BASELINE = "baseline"
    LEAK = "leak"
    DELIVERY_INTEGRITY = "delivery_integrity"
    RECONCILIATION = "reconciliation"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class ScenarioParams:
    """Optional per-run overrides.

    Every field must be an int, a str, or None — nothing float-valued, so that
    parameters cannot introduce non-reproducible arithmetic.
    """

    currency: str = DEFAULT_CURRENCY
    order_count: int | None = None
    attempt_count: int | None = None
    delay_seconds: int | None = None
    duplicate_count: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("order_count", "attempt_count", "delay_seconds", "duplicate_count"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field_name} must be an int, got {type(value).__name__}")
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative, got {value}")
        if len(self.currency) != 3:
            raise ValueError(f"currency must be a 3-letter ISO 4217 code, got {self.currency!r}")
