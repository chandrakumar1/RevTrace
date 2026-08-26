"""Deterministic money arithmetic.

The central property under test: a float can never enter a money path, and
identical inputs always produce identical outputs.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.money import (
    CurrencyMismatchError,
    MoneyError,
    Rounding,
    UnsupportedCurrencyError,
    apply_bps,
    clamp_non_negative,
    format_money,
    normalize_currency,
    require_same_currency,
    to_major,
    to_minor,
)


class TestFloatRejection:
    """Floats are refused at the boundary, not silently converted."""

    def test_to_minor_rejects_float(self) -> None:
        with pytest.raises(TypeError, match="float is not accepted"):
            to_minor(49.99)  # type: ignore[arg-type]

    def test_to_minor_rejects_float_even_when_whole(self) -> None:
        with pytest.raises(TypeError):
            to_minor(50.0)  # type: ignore[arg-type]

    def test_string_input_is_exact(self) -> None:
        """The classic float trap: 0.1 + 0.2 != 0.3. Strings avoid it entirely."""
        assert to_minor("0.1") + to_minor("0.2") == to_minor("0.3")


class TestConversion:
    @pytest.mark.parametrize(
        ("major", "expected_minor"),
        [("0", 0), ("1", 100), ("49.99", 4999), ("100.00", 10000), ("0.01", 1)],
    )
    def test_to_minor(self, major: str, expected_minor: int) -> None:
        assert to_minor(major, "INR") == expected_minor

    def test_round_trip(self) -> None:
        assert to_major(4999, "INR") == Decimal("49.99")

    def test_decimal_input(self) -> None:
        assert to_minor(Decimal("12.34")) == 1234

    def test_int_input(self) -> None:
        assert to_minor(75) == 7500

    def test_rejects_garbage(self) -> None:
        with pytest.raises(MoneyError):
            to_minor("not-a-number")


class TestRounding:
    def test_half_up(self) -> None:
        assert to_minor("0.005", rounding=Rounding.HALF_UP) == 1

    def test_down_truncates(self) -> None:
        assert to_minor("0.009", rounding=Rounding.DOWN) == 0

    def test_rounding_is_explicit_and_reproducible(self) -> None:
        for _ in range(100):
            assert to_minor("1.005", rounding=Rounding.HALF_EVEN) == 100


class TestCurrency:
    def test_normalize_uppercases(self) -> None:
        assert normalize_currency("inr") == "INR"

    def test_unsupported_currency_raises(self) -> None:
        with pytest.raises(UnsupportedCurrencyError):
            normalize_currency("XYZ")

    def test_same_currency_returns_code(self) -> None:
        assert require_same_currency("INR", "inr") == "INR"

    def test_mismatch_raises_never_coerces(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            require_same_currency("INR", "USD")

    def test_format(self) -> None:
        assert format_money(4999, "INR") == "49.99 INR"


class TestBasisPoints:
    """Rates are integer bps, so probabilities are exactly reproducible."""

    def test_full_rate(self) -> None:
        assert apply_bps(10_000, 10_000) == 10_000

    def test_half_rate(self) -> None:
        assert apply_bps(10_000, 5_000) == 5_000

    def test_zero_rate(self) -> None:
        assert apply_bps(10_000, 0) == 0

    def test_typical_recovery_probability(self) -> None:
        """4999 paise at a 37.5% recovery probability."""
        assert apply_bps(4999, 3750) == 1875

    def test_negative_bps_rejected(self) -> None:
        with pytest.raises(MoneyError):
            apply_bps(1000, -1)

    def test_result_is_always_int(self) -> None:
        assert isinstance(apply_bps(3333, 3333), int)

    def test_deterministic_across_calls(self) -> None:
        results = {apply_bps(123_456_789, 1234) for _ in range(50)}
        assert len(results) == 1


class TestClamping:
    def test_clamps_negative(self) -> None:
        assert clamp_non_negative(-500) == 0

    def test_leaves_positive(self) -> None:
        assert clamp_non_negative(500) == 500
