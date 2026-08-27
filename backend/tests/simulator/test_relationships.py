"""Entity graph consistency — no orphans, no impossible relationships."""

from __future__ import annotations

from simulator import simulate

from .conftest import SEED


class TestForeignKeyResolution:
    def test_customers_belong_to_a_generated_merchant(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        merchant_ids = {m.id for m in result.entities.merchants}
        for customer in result.entities.customers:
            assert customer.merchant_id in merchant_ids

    def test_orders_belong_to_a_generated_merchant(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        merchant_ids = {m.id for m in result.entities.merchants}
        for order in result.entities.orders:
            assert order.merchant_id in merchant_ids

    def test_orders_reference_a_generated_customer_or_none(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        customer_ids = {c.id for c in result.entities.customers}
        for order in result.entities.orders:
            assert order.customer_id is None or order.customer_id in customer_ids

    def test_attempts_belong_to_a_generated_order(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        order_ids = {o.id for o in result.entities.orders}
        for attempt in result.entities.payment_attempts:
            assert attempt.order_id in order_ids

    def test_events_reference_generated_entities(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        merchant_ids = {m.id for m in result.entities.merchants}
        customer_ids = {c.id for c in result.entities.customers}
        order_ids = {o.id for o in result.entities.orders}

        for delivery in result.deliveries:
            event = delivery.event
            assert event.merchant_id in merchant_ids
            assert event.customer_id is None or event.customer_id in customer_ids
            assert event.order_id is None or event.order_id in order_ids


class TestAttemptNumbering:
    def test_contiguous_from_one_per_order(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        by_order: dict[object, list[int]] = {}
        for attempt in result.entities.payment_attempts:
            by_order.setdefault(attempt.order_id, []).append(attempt.attempt_number)

        for numbers in by_order.values():
            assert sorted(numbers) == list(range(1, len(numbers) + 1))

    def test_attempt_amounts_match_their_order(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        by_id = {o.id: o for o in result.entities.orders}
        for attempt in result.entities.payment_attempts:
            order = by_id.get(attempt.order_id)
            if order is not None:
                assert attempt.amount == order.amount

    def test_later_attempts_occur_later(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        by_order: dict[object, list] = {}
        for attempt in result.entities.payment_attempts:
            by_order.setdefault(attempt.order_id, []).append(attempt)

        for attempts in by_order.values():
            ordered = sorted(attempts, key=lambda a: a.attempt_number)
            times = [a.attempted_at for a in ordered]
            assert times == sorted(times)


class TestUniqueness:
    def test_entity_ids_are_unique(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        for collection in (
            result.entities.merchants,
            result.entities.customers,
            result.entities.orders,
            result.entities.payment_attempts,
        ):
            ids = [item.id for item in collection]
            assert len(ids) == len(set(ids))

    def test_external_order_ids_are_unique(self, scenario_id: str) -> None:
        """Backs UNIQUE(merchant_id, external_order_id) in the Phase 1 schema."""
        result = simulate(scenario_id, seed=SEED)
        refs = [o.external_order_id for o in result.entities.orders]
        assert len(refs) == len(set(refs))

    def test_external_payment_ids_are_unique(self, scenario_id: str) -> None:
        """Backs UNIQUE(provider, external_payment_id)."""
        result = simulate(scenario_id, seed=SEED)
        refs = [a.external_payment_id for a in result.entities.payment_attempts]
        assert len(refs) == len(set(refs))

    def test_external_customer_ids_are_unique(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        refs = [c.external_customer_id for c in result.entities.customers]
        assert len(refs) == len(set(refs))


class TestProviderNeutrality:
    def test_provider_is_never_razorpay(self, scenario_id: str) -> None:
        """Nothing here came from Razorpay, and the data must not claim it did."""
        for attempt in simulate(scenario_id, seed=SEED).entities.payment_attempts:
            assert attempt.provider == "simulator"

    def test_no_razorpay_identifiers_anywhere(self, scenario_id: str) -> None:
        result = simulate(scenario_id, seed=SEED)
        for delivery in result.deliveries:
            assert "rzp_" not in str(delivery.event.payload)
        for attempt in result.entities.payment_attempts:
            assert not attempt.external_payment_id.startswith("rzp_")
