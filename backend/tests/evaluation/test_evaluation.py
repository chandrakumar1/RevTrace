"""Evaluation: TP/FP/FN, precision, recall, and regression fixtures.

Hermetic — no database, no HTTP. Ground truth is used **only** here, as the
answer key, and never reaches detection.

Every figure in this file is a **synthetic/demo measurement** over generated
data. It says the detectors behave correctly on the scenarios that exist; it is
not evidence of real-world accuracy, and must be labelled as such wherever it is
reported.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
from simulator.scenarios import all_scenarios

from app.models.enums import RiskType
from tests.evaluation.harness import (
    ANOMALY_TO_RISK_TYPE,
    AS_OF,
    Expectation,
    _ratio_bps,
    detect_for_scenario,
    detection_snapshot,
    evaluate_all,
    evaluate_scenario,
    expectations_for,
    fixture_path,
    read_fixture,
)

ALL_SCENARIOS = tuple(spec.id for spec in all_scenarios())

#: Scenarios where a leak is real and must be found.
LEAK_SCENARIOS = ("S04", "S04b", "S04c", "S05", "S06", "S08", "S09", "S10", "S12", "S12b")

#: Negative controls: no revenue was lost, so detection must stay silent.
CLEAN_SCENARIOS = ("S01", "S02", "S03", "S07", "S11", "S13", "S14")


class TestGroundTruthIsolation:
    """Layer two of three keeping the answer key out of detection."""

    def test_no_app_module_imports_the_harness(self) -> None:
        app_root = pathlib.Path(__file__).resolve().parents[2] / "app"
        offenders: list[str] = []

        for path in sorted(app_root.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for module in modules:
                    if module.startswith("tests") or "evaluation" in module:
                        offenders.append(f"{path.name} -> {module}")

        assert not offenders, f"app/ imports evaluation code: {offenders}"

    def test_no_app_module_imports_the_simulator(self) -> None:
        app_root = pathlib.Path(__file__).resolve().parents[2] / "app"
        for path in sorted(app_root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            assert "import simulator" not in source
            assert "from simulator" not in source

    def test_detection_signature_accepts_no_ground_truth(self) -> None:
        import inspect

        from app.engine.detectors import detect_for_merchant, detect_for_order

        for function in (detect_for_order, detect_for_merchant):
            params = set(inspect.signature(function).parameters)
            assert not params & {"ground_truth", "expected_risks", "expectations"}

    def test_snapshot_carries_no_ground_truth(self) -> None:
        rendered = json.dumps(detection_snapshot("S04"))
        for marker in ("ground_truth", "expected_risk", "narrative", "is_true_positive"):
            assert marker not in rendered


class TestExpectations:
    def test_leak_scenarios_expect_something(self) -> None:
        for scenario in LEAK_SCENARIOS:
            assert expectations_for(scenario), scenario

    def test_clean_scenarios_expect_nothing(self) -> None:
        for scenario in CLEAN_SCENARIOS:
            assert expectations_for(scenario) == (), scenario

    def test_anomaly_is_mapped_to_the_phase_3_risk_type(self) -> None:
        """S10 was an untyped anomaly in Phase 2; Phase 3 gave it a RiskType."""
        expectations = expectations_for("S10")
        assert len(expectations) == 1
        assert expectations[0].risk_type == RiskType.RECONCILIATION_MISMATCH.value
        assert expectations[0].source == "anomaly"

    def test_anomaly_expectation_is_zero_at_risk(self) -> None:
        assert expectations_for("S10")[0].amount_at_risk == 0

    def test_unmapped_anomaly_is_not_a_false_negative(self) -> None:
        """An anomaly with no risk type is not yet detectable, and is not scored."""
        from simulator.models import ExpectedAnomaly

        from tests.evaluation.harness import _expectation_from_anomaly

        assert _expectation_from_anomaly(ExpectedAnomaly("unknown_kind", None, "x")) is None

    def test_mapping_targets_are_real_risk_types(self) -> None:
        for risk_type in ANOMALY_TO_RISK_TYPE.values():
            assert risk_type in RiskType.values()


class TestPerScenarioEvaluation:
    @pytest.mark.parametrize("scenario", LEAK_SCENARIOS)
    def test_leak_is_detected(self, scenario: str) -> None:
        result = evaluate_scenario(scenario)
        assert result.true_positives == 1
        assert result.false_negatives == 0

    @pytest.mark.parametrize("scenario", LEAK_SCENARIOS)
    def test_leak_amount_is_exact(self, scenario: str) -> None:
        assert evaluate_scenario(scenario).amount_mismatches == 0

    @pytest.mark.parametrize("scenario", CLEAN_SCENARIOS)
    def test_negative_control_produces_no_false_positive(self, scenario: str) -> None:
        result = evaluate_scenario(scenario)
        assert result.false_positives == 0
        assert result.true_positives == 0

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS)
    def test_every_scenario_is_clean(self, scenario: str) -> None:
        assert evaluate_scenario(scenario).is_clean, evaluate_scenario(scenario)

    def test_delivery_pathology_scores_like_its_clean_counterpart(self) -> None:
        baseline = evaluate_scenario("S04")
        for scenario in ("S08", "S09", "S12b"):
            result = evaluate_scenario(scenario)
            assert result.true_positives == baseline.true_positives
            assert result.false_positives == baseline.false_positives


class TestAggregateMetrics:
    def test_perfect_scores_across_the_catalogue(self) -> None:
        aggregate = evaluate_all(ALL_SCENARIOS)

        assert aggregate.true_positives == 10
        assert aggregate.false_positives == 0
        assert aggregate.false_negatives == 0
        assert aggregate.amount_mismatches == 0

    def test_precision_and_recall(self) -> None:
        aggregate = evaluate_all(ALL_SCENARIOS)
        assert aggregate.precision_bps == 10_000
        assert aggregate.recall_bps == 10_000

    def test_negative_controls_contribute_to_the_precision_denominator(self) -> None:
        """Baselines are what make precision meaningful rather than trivial."""
        clean = evaluate_all(CLEAN_SCENARIOS)
        assert clean.false_positives == 0
        assert clean.precision_bps is None  # nothing detected, nothing to be wrong about

    def test_leaks_alone_give_full_recall(self) -> None:
        leaks = evaluate_all(LEAK_SCENARIOS)
        assert leaks.recall_bps == 10_000
        assert leaks.true_positives == 10

    def test_metrics_are_integers(self) -> None:
        aggregate = evaluate_all(ALL_SCENARIOS)
        for value in (aggregate.precision_bps, aggregate.recall_bps):
            assert isinstance(value, int) and not isinstance(value, bool)


class TestRatioArithmetic:
    """Integer basis points, half-up. No float ever enters the calculation."""

    def test_zero_denominator_is_none(self) -> None:
        assert _ratio_bps(0, 0) is None

    def test_perfect_ratio(self) -> None:
        assert _ratio_bps(10, 10) == 10_000

    def test_half_ratio(self) -> None:
        assert _ratio_bps(1, 2) == 5_000

    def test_rounds_half_up(self) -> None:
        assert _ratio_bps(1, 3) == 3_333
        assert _ratio_bps(2, 3) == 6_667

    def test_zero_numerator(self) -> None:
        assert _ratio_bps(0, 7) == 0

    def test_result_is_always_an_int(self) -> None:
        for numerator, denominator in ((1, 7), (5, 9), (13, 17)):
            assert isinstance(_ratio_bps(numerator, denominator), int)


class TestRegressionFixtures:
    """A behaviour change must fail loudly rather than drift unnoticed."""

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS)
    def test_fixture_exists(self, scenario: str) -> None:
        assert fixture_path(scenario).exists()

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS)
    def test_snapshot_matches_the_committed_fixture(self, scenario: str) -> None:
        assert detection_snapshot(scenario) == read_fixture(scenario)

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS)
    def test_snapshot_is_deterministic(self, scenario: str) -> None:
        assert detection_snapshot(scenario) == detection_snapshot(scenario)

    def test_fixtures_cover_every_scenario(self) -> None:
        stored = {path.stem.split("_seed")[0] for path in fixture_path("S04").parent.glob("*.json")}
        assert stored == set(ALL_SCENARIOS)

    def test_clean_fixtures_record_no_findings(self) -> None:
        for scenario in CLEAN_SCENARIOS:
            assert read_fixture(scenario)["findings"] == []

    def test_leak_fixtures_record_one_finding(self) -> None:
        for scenario in LEAK_SCENARIOS:
            assert len(read_fixture(scenario)["findings"]) == 1

    def test_fixture_records_the_evaluation_instant(self) -> None:
        assert read_fixture("S04")["as_of"] == AS_OF.isoformat().replace("+00:00", "Z")

    def test_fixture_money_is_integer_minor_units(self) -> None:
        for scenario in LEAK_SCENARIOS:
            amount = read_fixture(scenario)["findings"][0]["amount_at_risk_minor"]
            assert isinstance(amount, int) and not isinstance(amount, bool)

    def test_fixture_confidence_is_in_range(self) -> None:
        for scenario in LEAK_SCENARIOS:
            confidence = read_fixture(scenario)["findings"][0]["confidence_bps"]
            assert 0 <= confidence <= 10_000

    def test_reconciliation_fixture_is_zero_at_risk(self) -> None:
        assert read_fixture("S10")["findings"][0]["amount_at_risk_minor"] == 0

    def test_fixtures_are_valid_json_and_sorted(self) -> None:
        for scenario in ALL_SCENARIOS:
            raw = fixture_path(scenario).read_text(encoding="utf-8")
            parsed = json.loads(raw)
            assert raw == json.dumps(parsed, indent=2, sort_keys=True) + "\n"


class TestAuthorityBoundary:
    """Evaluation measures. It never authorises."""

    def test_findings_carry_no_recovery_or_policy_fields(self) -> None:
        for scenario in LEAK_SCENARIOS:
            for finding in detect_for_scenario(scenario):
                for forbidden in (
                    "approved",
                    "policy_status",
                    "recommended_action",
                    "expected_recovery",
                    "executed",
                ):
                    assert not hasattr(finding, forbidden)

    def test_snapshot_contains_no_authority_language(self) -> None:
        for scenario in LEAK_SCENARIOS:
            rendered = json.dumps(detection_snapshot(scenario))
            for forbidden in ("approved", "policy_status", "executed", "recovery_case"):
                assert forbidden not in rendered

    def test_harness_writes_nothing_to_a_database(self) -> None:
        import tests.evaluation.harness as harness

        source = pathlib.Path(harness.__file__).read_text(encoding="utf-8")
        for banned in ("app.db", "sqlalchemy", "Session", "session"):
            assert banned not in source

    def test_expectation_is_a_plain_value(self) -> None:
        expectation = expectations_for("S04")[0]
        assert isinstance(expectation, Expectation)
        assert not hasattr(expectation, "confidence")
        assert not hasattr(expectation, "recommended_action")


class TestSyntheticLabelling:
    """Metrics over generated data must never be presented as real accuracy."""

    def test_harness_documents_that_metrics_are_synthetic(self) -> None:
        import tests.evaluation.harness as harness

        assert harness.__doc__ is not None
        assert "synthetic/demo measurement" in harness.__doc__

    def test_this_module_documents_it_too(self) -> None:
        import tests.evaluation.test_evaluation as module

        assert module.__doc__ is not None
        assert "synthetic/demo measurement" in module.__doc__
