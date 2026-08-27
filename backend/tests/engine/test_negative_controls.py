"""Cross-detector negative controls — the whole registry at once.

Individual detectors have their own guards. This file asserts the property that
matters end to end: running **every** detector over a scenario that is not a
revenue leak must produce **nothing at all**.

Without these, precision is unmeasurable and a detector that fires on everything
would look perfect.
"""

from __future__ import annotations

import pytest

from app.engine.detectors import DETECTION_RULES, detect_for_merchant, detect_for_order
from app.models.enums import RiskType
from app.services.tracing.reconstruction import reconstruct_merchant
from tests.engine.conftest import AS_OF, order_timeline
from tests.tracing.conftest import merchant_id_of, scenario_events

#: Scenarios where no revenue was lost. Every one must be completely silent.
CLEAN_SCENARIOS = ("S01", "S02", "S03", "S07", "S11", "S13")

#: Scenarios where a leak is real.
LEAK_SCENARIOS = ("S04", "S04b", "S04c", "S05", "S08", "S09", "S12", "S12b")


class TestBaselinesProduceNothing:
    """The precision floor."""

    @pytest.mark.parametrize("scenario", CLEAN_SCENARIOS)
    def test_no_findings_at_all(self, scenario: str) -> None:
        assert detect_for_order(order_timeline(scenario), AS_OF) == ()

    @pytest.mark.parametrize("scenario", CLEAN_SCENARIOS)
    def test_no_findings_at_merchant_level(self, scenario: str) -> None:
        timeline = reconstruct_merchant(merchant_id_of(scenario), scenario_events(scenario))
        assert detect_for_merchant(timeline, AS_OF) == ()

    def test_healthy_payment_is_silent(self) -> None:
        assert detect_for_order(order_timeline("S01"), AS_OF) == ()

    def test_retry_success_is_silent(self) -> None:
        """The single most important false-positive guard in the product."""
        assert detect_for_order(order_timeline("S02"), AS_OF) == ()

    def test_multiple_attempts_then_success_is_silent(self) -> None:
        assert detect_for_order(order_timeline("S03"), AS_OF) == ()

    def test_duplicate_delivery_alone_is_silent(self) -> None:
        """A redelivered webhook is not a duplicate payment."""
        assert detect_for_order(order_timeline("S07"), AS_OF) == ()

    def test_recovery_success_is_silent(self) -> None:
        """Recovered revenue is not permanently lost revenue."""
        assert detect_for_order(order_timeline("S11"), AS_OF) == ()

    def test_legitimate_refund_is_silent(self) -> None:
        assert detect_for_order(order_timeline("S13"), AS_OF) == ()


class TestConfusionsThatMustNotHappen:
    """Each pairing names a specific way a detector could be wrong."""

    def test_retry_success_is_not_repeated_failure(self) -> None:
        assert order_timeline("S02").failed_attempts
        assert detect_for_order(order_timeline("S02"), AS_OF) == ()

    def test_refund_is_not_revenue_leakage(self) -> None:
        assert order_timeline("S13").has_refund
        assert detect_for_order(order_timeline("S13"), AS_OF) == ()

    def test_duplicate_delivery_is_not_duplicate_payment(self) -> None:
        duplicated = order_timeline("S07")
        assert duplicated.integrity.duplicate_deliveries == 2
        assert detect_for_order(duplicated, AS_OF) == ()

    def test_out_of_order_delivery_is_not_a_causal_failure(self) -> None:
        """Arrival order must never create a finding that content does not."""
        scrambled = detect_for_order(order_timeline("S08"), AS_OF)
        clean = detect_for_order(order_timeline("S04"), AS_OF)
        assert len(scrambled) == len(clean)

    def test_delayed_delivery_is_not_a_missing_event(self) -> None:
        delayed = detect_for_order(order_timeline("S09"), AS_OF)
        clean = detect_for_order(order_timeline("S04"), AS_OF)

        assert len(delayed) == len(clean)
        assert {f.risk_type for f in delayed} == {f.risk_type for f in clean}

    def test_captured_but_unreconciled_is_not_a_payment_failure(self) -> None:
        findings = detect_for_order(order_timeline("S10"), AS_OF)
        assert {f.risk_type for f in findings} == {RiskType.RECONCILIATION_MISMATCH.value}


class TestLeaksAreStillCaught:
    """Suppression must not have been achieved by silencing everything."""

    @pytest.mark.parametrize("scenario", LEAK_SCENARIOS)
    def test_a_finding_is_produced(self, scenario: str) -> None:
        assert detect_for_order(order_timeline(scenario), AS_OF)

    @pytest.mark.parametrize("scenario", LEAK_SCENARIOS)
    def test_exactly_one_finding(self, scenario: str) -> None:
        """No scenario should trip two detectors at once."""
        assert len(detect_for_order(order_timeline(scenario), AS_OF)) == 1

    def test_reconciliation_scenario_produces_one_finding(self) -> None:
        assert len(detect_for_order(order_timeline("S10"), AS_OF)) == 1

    def test_subscription_failure_is_caught_at_merchant_level(self) -> None:
        timeline = reconstruct_merchant(merchant_id_of("S06"), scenario_events("S06"))
        findings = detect_for_merchant(timeline, AS_OF)

        assert len(findings) == 1
        assert findings[0].risk_type == RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value


class TestMixedBaseline:
    """S14 is 20 orders at ~85% success — the realistic precision test."""

    def test_only_unpaid_orders_produce_findings(self) -> None:
        timeline = reconstruct_merchant(merchant_id_of("S14"), scenario_events("S14"))
        findings = detect_for_merchant(timeline, AS_OF)

        flagged = {f.order_id for f in findings}
        for order in timeline.orders:
            if order.reached_terminal_success:
                assert order.order_id not in flagged

    def test_single_isolated_failures_do_not_fire(self) -> None:
        """S14's failures are one attempt each — below the threshold."""
        timeline = reconstruct_merchant(merchant_id_of("S14"), scenario_events("S14"))
        findings = detect_for_merchant(timeline, AS_OF)

        assert all(f.risk_type != RiskType.REPEATED_PAYMENT_FAILURE.value for f in findings)

    def test_no_amount_is_double_counted(self) -> None:
        timeline = reconstruct_merchant(merchant_id_of("S14"), scenario_events("S14"))
        findings = detect_for_merchant(timeline, AS_OF)

        for finding in findings:
            order = timeline.order(finding.order_id) if finding.order_id else None
            if order is not None:
                assert finding.amount_at_risk <= order.amount_minor


class TestRegistry:
    def test_every_rule_is_versioned(self) -> None:
        assert all(rule.endswith(".v1") for rule in DETECTION_RULES)

    def test_rules_are_unique(self) -> None:
        assert len(DETECTION_RULES) == len(set(DETECTION_RULES))

    def test_payment_degradation_is_deferred(self) -> None:
        """Deferred until a positive scenario exists to test it against."""
        assert not any("degradation" in rule for rule in DETECTION_RULES)

    def test_results_are_deterministically_ordered(self) -> None:
        timeline = reconstruct_merchant(merchant_id_of("S14"), scenario_events("S14"))
        first = detect_for_merchant(timeline, AS_OF)
        second = detect_for_merchant(timeline, AS_OF)

        assert [f.natural_key for f in first] == [f.natural_key for f in second]

    def test_every_finding_uses_a_registered_rule(self) -> None:
        for scenario in (*LEAK_SCENARIOS, "S10"):
            for finding in detect_for_order(order_timeline(scenario), AS_OF):
                assert finding.detection_rule in DETECTION_RULES

    def test_every_risk_type_is_a_phase_1_enum_value(self) -> None:
        for scenario in (*LEAK_SCENARIOS, "S10"):
            for finding in detect_for_order(order_timeline(scenario), AS_OF):
                assert finding.risk_type in RiskType.values()


class TestPurity:
    """Detection identifies risk. It never authorises anything."""

    def test_no_detector_module_touches_the_database(self) -> None:
        import ast
        import pathlib

        import app.engine.detectors as package

        root = pathlib.Path(package.__file__).parent
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]

                for module in modules:
                    assert not module.startswith("app.db"), f"{path.name} imports {module}"
                    assert not module.startswith("sqlalchemy"), f"{path.name} imports {module}"
                    assert module not in {"httpx", "requests"}, f"{path.name} imports {module}"

    def test_no_detector_reads_a_clock(self) -> None:
        import ast
        import pathlib

        import app.engine.detectors as package

        banned = {"datetime.now", "datetime.utcnow", "time.time"}

        def dotted(node: ast.expr) -> str | None:
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute):
                base = dotted(node.value)
                return f"{base}.{node.attr}" if base else None
            return None

        root = pathlib.Path(package.__file__).parent
        for path in sorted(root.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    assert dotted(node.func) not in banned, f"{path.name}:{node.lineno}"

    def test_findings_carry_no_authority_fields(self) -> None:
        for scenario in LEAK_SCENARIOS:
            for finding in detect_for_order(order_timeline(scenario), AS_OF):
                for forbidden in (
                    "approved",
                    "policy_status",
                    "recommended_action",
                    "expected_recovery",
                    "executed",
                ):
                    assert not hasattr(finding, forbidden)
