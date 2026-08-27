"""Scenario catalog: structure, coverage, and per-scenario expectations."""

from __future__ import annotations

import pytest
from simulator import simulate
from simulator.config import ScenarioCategory
from simulator.scenarios import SCENARIO_REGISTRY, all_scenarios, scenarios_by_category

from app.models.enums import EventType, OrderStatus, PaymentStatus, RiskType

from .conftest import SEED


def _types(result: object) -> list[str]:
    from simulator.models import SimulationResult

    assert isinstance(result, SimulationResult)
    return [e.event_type for e in result.events_in_causal_order]


class TestRegistry:
    def test_ids_are_unique(self) -> None:
        ids = [spec.id for spec in all_scenarios()]
        assert len(ids) == len(set(ids))

    def test_names_are_unique(self) -> None:
        names = [spec.name for spec in all_scenarios()]
        assert len(names) == len(set(names))

    def test_every_spec_documents_itself(self) -> None:
        for spec in all_scenarios():
            assert spec.description
            assert spec.purpose

    def test_every_category_is_represented(self) -> None:
        for category in ScenarioCategory:
            assert scenarios_by_category(category), f"{category.value} has no scenarios"

    def test_registry_matches_the_spec_list(self) -> None:
        assert set(SCENARIO_REGISTRY) == {spec.id for spec in all_scenarios()}

    def test_every_scenario_produces_events(self, scenario_id: str) -> None:
        assert simulate(scenario_id, seed=SEED).deliveries


class TestBaselineScenarios:
    """Baselines must produce no risk. Precision is unmeasurable without them."""

    @pytest.mark.parametrize("scenario", ["S01", "S02", "S03", "S13"])
    def test_no_expected_risks(self, scenario: str) -> None:
        assert simulate(scenario, seed=SEED).ground_truth.expected_risks == ()

    def test_healthy_payment_reaches_paid(self) -> None:
        result = simulate("S01", seed=SEED)
        assert EventType.ORDER_PAID.value in _types(result)
        assert result.entities.orders[0].status == OrderStatus.PAID.value

    def test_retry_success_has_one_failure_then_capture(self) -> None:
        result = simulate("S02", seed=SEED)
        statuses = [a.status for a in result.entities.payment_attempts]
        assert PaymentStatus.FAILED.value in statuses
        assert PaymentStatus.CAPTURED.value in statuses
        assert result.entities.orders[0].status == OrderStatus.PAID.value

    def test_multiple_attempts_eventually_succeeds(self) -> None:
        result = simulate("S03", seed=SEED)
        assert len(result.entities.payment_attempts) == 3
        assert result.entities.payment_attempts[-1].status == PaymentStatus.CAPTURED.value

    def test_mixed_baseline_has_a_realistic_success_rate(self) -> None:
        result = simulate("S14", seed=SEED)
        attempts = result.entities.payment_attempts
        captured = sum(1 for a in attempts if a.status == PaymentStatus.CAPTURED.value)
        assert 0.6 <= captured / len(attempts) <= 0.98


class TestLeakScenarios:
    def test_repeated_failure_reports_the_right_risk(self) -> None:
        truth = simulate("S04", seed=SEED).ground_truth
        assert len(truth.expected_risks) == 1
        assert truth.expected_risks[0].risk_type == RiskType.REPEATED_PAYMENT_FAILURE.value

    def test_repeated_failure_amount_matches_the_order(self) -> None:
        result = simulate("S04", seed=SEED)
        assert result.ground_truth.expected_risks[0].amount_at_risk == (
            result.entities.orders[0].amount
        )

    def test_repeated_failure_never_reaches_paid(self) -> None:
        result = simulate("S04", seed=SEED)
        assert EventType.ORDER_PAID.value not in _types(result)
        assert result.entities.orders[0].status == OrderStatus.ATTEMPTED.value

    def test_high_value_risk_exceeds_typical_risk(self) -> None:
        typical = simulate("S04", seed=SEED).ground_truth.expected_risks[0].amount_at_risk
        high = simulate("S04b", seed=SEED).ground_truth.expected_risks[0].amount_at_risk
        assert high > typical

    def test_upi_timeouts_use_timeout_status(self) -> None:
        result = simulate("S04c", seed=SEED)
        assert all(
            a.status == PaymentStatus.TIMEOUT.value for a in result.entities.payment_attempts
        )
        assert all(a.payment_method == "upi" for a in result.entities.payment_attempts)

    def test_abandonment_has_no_payment_attempts(self) -> None:
        result = simulate("S05", seed=SEED)
        assert result.entities.payment_attempts == ()
        assert EventType.PAYMENT_ATTEMPTED.value not in _types(result)

    def test_abandonment_reports_the_right_risk(self) -> None:
        truth = simulate("S05", seed=SEED).ground_truth
        assert truth.expected_risks[0].risk_type == RiskType.CHECKOUT_ABANDONMENT.value

    def test_subscription_failure_halts(self) -> None:
        types = _types(simulate("S06", seed=SEED))
        assert EventType.SUBSCRIPTION_CHARGED.value in types
        assert EventType.SUBSCRIPTION_PAYMENT_FAILED.value in types
        assert types[-1] == EventType.SUBSCRIPTION_HALTED.value

    def test_subscription_events_have_no_order(self) -> None:
        for event in simulate("S06", seed=SEED).events_in_causal_order:
            assert event.order_id is None

    def test_refund_is_not_a_leak(self) -> None:
        result = simulate("S13", seed=SEED)
        assert EventType.REFUND_CREATED.value in _types(result)
        assert result.ground_truth.expected_risks == ()
        assert result.entities.orders[0].status == OrderStatus.REFUNDED.value


class TestReconciliationScenario:
    def test_captured_but_never_paid(self) -> None:
        types = _types(simulate("S10", seed=SEED))
        assert EventType.PAYMENT_CAPTURED.value in types
        assert EventType.ORDER_PAID.value not in types

    def test_recorded_as_an_untyped_anomaly(self) -> None:
        """No Phase 1 RiskType describes this, so it must not claim one."""
        truth = simulate("S10", seed=SEED).ground_truth
        assert truth.expected_risks == ()
        assert len(truth.expected_anomalies) == 1
        assert truth.expected_anomalies[0].anomaly_kind == "payment_captured_order_not_reconciled"

    def test_order_is_stuck_at_attempted(self) -> None:
        result = simulate("S10", seed=SEED)
        assert result.entities.orders[0].status == OrderStatus.ATTEMPTED.value


class TestRecoveryScenarios:
    def test_recovery_success_ends_paid(self) -> None:
        result = simulate("S11", seed=SEED)
        types = _types(result)
        assert EventType.RECOVERY_ACTION_EXECUTED.value in types
        assert EventType.RECOVERY_SUCCEEDED.value in types
        assert result.entities.orders[0].status == OrderStatus.PAID.value
        assert result.ground_truth.expected_risks == ()

    def test_recovery_failure_leaves_revenue_lost(self) -> None:
        result = simulate("S12", seed=SEED)
        types = _types(result)
        assert EventType.RECOVERY_ACTION_EXECUTED.value in types
        assert EventType.RECOVERY_FAILED.value in types
        assert EventType.ORDER_PAID.value not in types
        assert result.ground_truth.expected_risks

    def test_recovery_ordering(self) -> None:
        events = simulate("S11", seed=SEED).events_in_causal_order
        executed = next(
            e for e in events if e.event_type == EventType.RECOVERY_ACTION_EXECUTED.value
        )
        succeeded = next(e for e in events if e.event_type == EventType.RECOVERY_SUCCEEDED.value)
        assert succeeded.occurred_at > executed.occurred_at


class TestAuthorityBoundary:
    """ADR 0005 — the simulator generates no risk or recovery entities."""

    def test_no_recovery_or_risk_entities_are_produced(self, scenario_id: str) -> None:
        entities = simulate(scenario_id, seed=SEED).entities
        assert set(vars(entities) if hasattr(entities, "__dict__") else {}) <= set()
        for attribute in ("recovery_cases", "recovery_actions", "revenue_risks", "audit_events"):
            assert not hasattr(entities, attribute)

    def test_ground_truth_states_no_scores_or_recommendations(self, scenario_id: str) -> None:
        truth = simulate(scenario_id, seed=SEED).ground_truth
        for risk in truth.expected_risks:
            assert not hasattr(risk, "confidence")
            assert not hasattr(risk, "confidence_bps")
            assert not hasattr(risk, "recommended_action")
            assert not hasattr(risk, "expected_recovery")

    def test_no_approval_is_fabricated(self, scenario_id: str) -> None:
        for delivery in simulate(scenario_id, seed=SEED).deliveries:
            payload = delivery.event.payload
            assert "approved" not in payload
            assert "policy_status" not in payload
            assert "approved_by" not in payload
