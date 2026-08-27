"""Money in generated data is always integer minor units (ADR 0001)."""

from __future__ import annotations

import pytest
from simulator import simulate
from simulator.config import (
    HIGH_VALUE_ORDER_PAISE,
    SUBSCRIPTION_CHARGE_PAISE,
    TYPICAL_ORDER_PAISE,
)

from app.core.money import format_money, to_major, to_minor

from .conftest import ALL_SCENARIO_IDS, SEED


def _all_amounts(result: object) -> list[tuple[str, object]]:
    """Every monetary value anywhere in a result, labelled for diagnostics."""
    from simulator.models import SimulationResult

    assert isinstance(result, SimulationResult)
    amounts: list[tuple[str, object]] = []

    for customer in result.entities.customers:
        amounts.append(
            (f"customer[{customer.external_customer_id}].lifetime_value", customer.lifetime_value)
        )
    for order in result.entities.orders:
        amounts.append((f"order[{order.external_order_id}].amount", order.amount))
    for attempt in result.entities.payment_attempts:
        amounts.append((f"attempt[{attempt.external_payment_id}].amount", attempt.amount))
    for delivery in result.deliveries:
        for key, value in delivery.event.payload.items():
            if key.endswith("_minor"):
                amounts.append((f"payload[{delivery.event.external_event_id}].{key}", value))
    for risk in result.ground_truth.expected_risks:
        amounts.append((f"risk[{risk.risk_type}].amount_at_risk", risk.amount_at_risk))

    return amounts


class TestIntegerMinorUnits:
    def test_every_amount_is_an_int(self, scenario_id: str) -> None:
        for label, value in _all_amounts(simulate(scenario_id, seed=SEED)):
            assert isinstance(value, int), f"{label} is {type(value).__name__}, not int"

    def test_no_amount_is_a_bool(self, scenario_id: str) -> None:
        for label, value in _all_amounts(simulate(scenario_id, seed=SEED)):
            assert not isinstance(value, bool), f"{label} is a bool"

    def test_no_amount_is_a_float(self, scenario_id: str) -> None:
        for label, value in _all_amounts(simulate(scenario_id, seed=SEED)):
            assert not isinstance(value, float), f"{label} is a float"

    def test_every_amount_is_non_negative(self, scenario_id: str) -> None:
        for label, value in _all_amounts(simulate(scenario_id, seed=SEED)):
            assert isinstance(value, int) and value >= 0, f"{label} is negative"

    @pytest.mark.parametrize("seed", [0, 1, 7, 42, 999, 100_000])
    def test_holds_across_many_seeds(self, seed: int) -> None:
        for scenario in ALL_SCENARIO_IDS:
            for label, value in _all_amounts(simulate(scenario, seed=seed)):
                assert isinstance(value, int) and not isinstance(value, bool), label


class TestCurrency:
    def test_orders_carry_a_three_letter_currency(self, scenario_id: str) -> None:
        for order in simulate(scenario_id, seed=SEED).entities.orders:
            assert len(order.currency) == 3

    def test_attempt_currency_matches_its_order(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        by_id = {order.id: order for order in result.entities.orders}
        for attempt in result.entities.payment_attempts:
            order = by_id.get(attempt.order_id)
            if order is not None:
                assert attempt.currency == order.currency

    def test_payload_money_is_paired_with_currency(self, scenario_id: str) -> None:
        for delivery in simulate(scenario_id, seed=SEED).deliveries:
            payload = delivery.event.payload
            if any(key.endswith("_minor") for key in payload):
                assert "currency" in payload


class TestAmountRanges:
    def test_typical_orders_fall_in_range(self) -> None:
        low, high, _ = TYPICAL_ORDER_PAISE
        for seed in range(20):
            amount = simulate("S04", seed=seed).entities.orders[0].amount
            assert low <= amount < high

    def test_high_value_orders_are_materially_larger(self) -> None:
        low, _, _ = HIGH_VALUE_ORDER_PAISE
        assert simulate("S04b", seed=SEED).entities.orders[0].amount >= low

    def test_subscription_charges_fall_in_range(self) -> None:
        low, high, _ = SUBSCRIPTION_CHARGE_PAISE
        risk = simulate("S06", seed=SEED).ground_truth.expected_risks[0]
        # Ground truth totals two failed cycles.
        assert low * 2 <= risk.amount_at_risk < high * 2

    def test_amounts_are_whole_rupees(self) -> None:
        """Generated amounts use a 100-paise step, so they are whole rupees."""
        for seed in range(10):
            assert simulate("S04", seed=seed).entities.orders[0].amount % 100 == 0


class TestPhase1HelperInterop:
    """Generated amounts round-trip through the Phase 1 money helpers."""

    def test_round_trip_through_helpers(self) -> None:
        amount = simulate("S04", seed=SEED).entities.orders[0].amount
        assert to_minor(to_major(amount, "INR"), "INR") == amount

    def test_formats_without_error(self) -> None:
        amount = simulate("S04", seed=SEED).entities.orders[0].amount
        assert format_money(amount, "INR").endswith(" INR")

    def test_refund_never_exceeds_captured_amount(self) -> None:
        result = simulate("S13", seed=SEED)
        order = result.entities.orders[0]
        refunds = [
            d.event.payload["amount_minor"]
            for d in result.deliveries
            if d.event.event_type == "refund.created"
        ]
        assert refunds
        for refund in refunds:
            assert isinstance(refund, int) and refund <= order.amount
