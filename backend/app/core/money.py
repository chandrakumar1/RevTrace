"""Deterministic money arithmetic in integer minor units (ADR 0001).

Every value here is an ``int`` count of minor units — paise for INR. Floats are
rejected at the boundary rather than converted, because a float that enters a
money path is not reproducible, and every downstream expected-recovery figure
must be exactly reproducible from the same inputs.

This module is deliberately LLM-free and network-free: it is deterministic
code, and the risk/recovery/policy engines built on it in Phases 3, 6, and 7
must remain so.

Rounding is explicit. There is no default rounding mode; callers state one.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum

#: Minor units per major unit for the currencies RevTrace handles.
#: Zero-decimal currencies (JPY, KRW) would need 1 here; none are supported yet.
MINOR_UNITS_PER_MAJOR: dict[str, int] = {
    "INR": 100,
    "USD": 100,
    "EUR": 100,
    "GBP": 100,
}

BASIS_POINTS_DENOMINATOR = 10_000


class Rounding(StrEnum):
    DOWN = "down"
    HALF_UP = "half_up"
    HALF_EVEN = "half_even"


_DECIMAL_ROUNDING = {
    Rounding.DOWN: ROUND_DOWN,
    Rounding.HALF_UP: ROUND_HALF_UP,
    Rounding.HALF_EVEN: ROUND_HALF_EVEN,
}


class MoneyError(ValueError):
    """Base class for money errors."""


class CurrencyMismatchError(MoneyError):
    """Two amounts in different currencies were combined."""


class UnsupportedCurrencyError(MoneyError):
    """A currency with no known minor-unit scale."""


def normalize_currency(currency: str) -> str:
    code = (currency or "").strip().upper()
    if code not in MINOR_UNITS_PER_MAJOR:
        raise UnsupportedCurrencyError(f"Unsupported currency: {currency!r}")
    return code


def require_same_currency(a: str, b: str) -> str:
    """Return the shared currency, or raise. Never silently coerces."""
    ca, cb = normalize_currency(a), normalize_currency(b)
    if ca != cb:
        raise CurrencyMismatchError(f"Currency mismatch: {ca} vs {cb}")
    return ca


def to_minor(
    amount: str | int | Decimal,
    currency: str = "INR",
    *,
    rounding: Rounding = Rounding.HALF_UP,
) -> int:
    """Convert a major-unit amount to integer minor units.

    Floats are rejected outright — pass a str or Decimal. Accepting a float
    here would silently import binary representation error into a money path.
    """
    if isinstance(amount, float):
        raise TypeError(
            "float is not accepted in money paths; pass str or Decimal "
            "(e.g. to_minor('49.99'), not to_minor(49.99))"
        )

    code = normalize_currency(currency)
    scale = MINOR_UNITS_PER_MAJOR[code]

    try:
        value = Decimal(amount) if not isinstance(amount, Decimal) else amount
    except (InvalidOperation, TypeError) as exc:
        raise MoneyError(f"Not a valid monetary amount: {amount!r}") from exc

    scaled = value * scale
    return int(scaled.to_integral_value(rounding=_DECIMAL_ROUNDING[rounding]))


def to_major(minor: int, currency: str = "INR") -> Decimal:
    """Convert integer minor units back to a major-unit Decimal. Exact."""
    code = normalize_currency(currency)
    return Decimal(minor) / Decimal(MINOR_UNITS_PER_MAJOR[code])


def format_money(minor: int, currency: str = "INR") -> str:
    """Human-readable rendering, e.g. '4999' INR -> '49.99 INR'."""
    code = normalize_currency(currency)
    scale = MINOR_UNITS_PER_MAJOR[code]
    digits = len(str(scale)) - 1
    return f"{to_major(minor, code):.{digits}f} {code}"


def apply_bps(minor: int, bps: int, *, rounding: Rounding = Rounding.HALF_EVEN) -> int:
    """Apply a basis-point rate to a minor-unit amount, returning minor units.

    Rates are integers in basis points rather than floats so that a probability
    or discount is exactly reproducible. 10000 bps == 100%.
    """
    if bps < 0:
        raise MoneyError(f"Basis points must be non-negative, got {bps}")

    exact = Decimal(minor) * Decimal(bps) / Decimal(BASIS_POINTS_DENOMINATOR)
    return int(exact.to_integral_value(rounding=_DECIMAL_ROUNDING[rounding]))


def add(a: int, b: int) -> int:
    """Integer addition, kept explicit so money math reads as money math."""
    return a + b


def subtract(a: int, b: int) -> int:
    return a - b


def clamp_non_negative(minor: int) -> int:
    """Floor at zero. Used where a negative amount is meaningless, not an error."""
    return max(0, minor)
