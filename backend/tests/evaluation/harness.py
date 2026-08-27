"""Evaluation harness — measuring detection against simulator ground truth.

**This module lives in tests/ on purpose.** Ground truth is the answer key. It
must never be importable from `app/`, and a test asserts that it isn't. The
three layers keeping it out of production remain: the ingestion schema rejects
it, nothing under `app/` imports this package, and no detector signature accepts
it.

The harness is hermetic. It simulates, reconstructs, and detects entirely in
memory — no database, no HTTP. The persisted path is already covered by the M7
and M8 integration tests; what is being measured here is the *decision quality*
of the deterministic engine, which does not depend on storage.

Metrics are integer basis points, not floats. The whole project compares
thresholds exactly, and a precision figure that drifts in the last bit would
undermine the regression fixtures.

Every number produced here is a **synthetic/demo measurement** over generated
data. It is not evidence of real-world accuracy and must be labelled as such
wherever it is reported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from simulator import simulate
from simulator.models import ExpectedAnomaly, ExpectedRisk

from app.engine.detectors import detect_for_merchant
from app.engine.detectors.base import RiskFinding
from app.models.enums import RiskType
from app.services.tracing.reconstruction import reconstruct_merchant

#: Fixed evaluation instant. Detectors never read a clock, so this is supplied.
AS_OF = datetime(2026, 6, 1, tzinfo=UTC)

SEED = 42

FIXTURE_DIR = Path(__file__).parent / "fixtures"

#: Phase 2 recorded S10 as an untyped anomaly because no RiskType described it.
#: Phase 3 added `reconciliation_mismatch` (ADR 0007), so evaluation maps the
#: anomaly onto the risk type that now exists. The simulator is unchanged —
#: this mapping lives here, in the evaluation layer, exactly where a change in
#: vocabulary should be reconciled.
ANOMALY_TO_RISK_TYPE: dict[str, str] = {
    "payment_captured_order_not_reconciled": RiskType.RECONCILIATION_MISMATCH.value,
}

#: An anomaly carries no amount. The reconciliation risk is defined as zero at
#: risk — the money arrived — so the expectation is zero too.
ANOMALY_EXPECTED_AMOUNT = 0


@dataclass(frozen=True, slots=True)
class Expectation:
    """One thing a correct detector should find, normalised across both
    ground-truth shapes (typed risk and untyped anomaly)."""

    risk_type: str
    amount_at_risk: int
    order_ref: str | None
    source: str  # "risk" or "anomaly"


@dataclass(frozen=True, slots=True)
class ScenarioEvaluation:
    """How detection scored on one scenario."""

    scenario_id: str
    seed: int
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    #: Correctly identified, but with the wrong amount. Tracked separately so a
    #: wrong number is never hidden inside a passing precision score.
    amount_mismatches: int = 0
    matched: tuple[str, ...] = ()
    unmatched_expected: tuple[str, ...] = ()
    unmatched_detected: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        return (
            self.false_positives == 0 and self.false_negatives == 0 and self.amount_mismatches == 0
        )


@dataclass(frozen=True, slots=True)
class AggregateEvaluation:
    """Totals across a set of scenarios."""

    scenarios: tuple[ScenarioEvaluation, ...] = field(default_factory=tuple)

    @property
    def true_positives(self) -> int:
        return sum(s.true_positives for s in self.scenarios)

    @property
    def false_positives(self) -> int:
        return sum(s.false_positives for s in self.scenarios)

    @property
    def false_negatives(self) -> int:
        return sum(s.false_negatives for s in self.scenarios)

    @property
    def amount_mismatches(self) -> int:
        return sum(s.amount_mismatches for s in self.scenarios)

    @property
    def precision_bps(self) -> int | None:
        """TP / (TP + FP), in basis points. None when nothing was detected."""
        return _ratio_bps(self.true_positives, self.true_positives + self.false_positives)

    @property
    def recall_bps(self) -> int | None:
        """TP / (TP + FN), in basis points. None when nothing was expected."""
        return _ratio_bps(self.true_positives, self.true_positives + self.false_negatives)


def _ratio_bps(numerator: int, denominator: int) -> int | None:
    """Integer basis points, half-up. No float ever enters the calculation."""
    if denominator == 0:
        return None
    return (numerator * 10_000 + denominator // 2) // denominator


# -- normalising ground truth --------------------------------------------


def _expectation_from_risk(risk: ExpectedRisk) -> Expectation:
    return Expectation(
        risk_type=risk.risk_type,
        amount_at_risk=risk.amount_at_risk,
        order_ref=risk.order_ref,
        source="risk",
    )


def _expectation_from_anomaly(anomaly: ExpectedAnomaly) -> Expectation | None:
    risk_type = ANOMALY_TO_RISK_TYPE.get(anomaly.anomaly_kind)
    if risk_type is None:
        # An anomaly with no corresponding risk type is not yet detectable and
        # is deliberately not counted as a false negative.
        return None
    return Expectation(
        risk_type=risk_type,
        amount_at_risk=ANOMALY_EXPECTED_AMOUNT,
        order_ref=anomaly.order_ref,
        source="anomaly",
    )


def expectations_for(scenario: str, seed: int = SEED) -> tuple[Expectation, ...]:
    """The answer key for one scenario, normalised."""
    truth = simulate(scenario, seed=seed).ground_truth

    items = [_expectation_from_risk(risk) for risk in truth.expected_risks]
    for anomaly in truth.expected_anomalies:
        mapped = _expectation_from_anomaly(anomaly)
        if mapped is not None:
            items.append(mapped)

    return tuple(items)


# -- running detection ----------------------------------------------------


def detect_for_scenario(scenario: str, seed: int = SEED) -> tuple[RiskFinding, ...]:
    """Detection output for one scenario. Ground truth is never consulted."""
    result = simulate(scenario, seed=seed)
    timeline = reconstruct_merchant(
        result.entities.merchants[0].id, [d.event for d in result.deliveries]
    )
    return detect_for_merchant(timeline, AS_OF)


# -- matching -------------------------------------------------------------


def _matches(expectation: Expectation, finding: RiskFinding) -> bool:
    """Same risk type, and the same order where both name one.

    Subscription expectations carry no `order_ref` in Phase 2 ground truth while
    detection reports the subscription reference, so the order check applies
    only when both sides have one.
    """
    if expectation.risk_type != finding.risk_type:
        return False
    if expectation.order_ref is None or finding.order_ref is None:
        return True
    return expectation.order_ref == finding.order_ref


def evaluate_scenario(scenario: str, seed: int = SEED) -> ScenarioEvaluation:
    """Score detection against ground truth for one scenario."""
    expectations = list(expectations_for(scenario, seed))
    findings = list(detect_for_scenario(scenario, seed))

    matched: list[str] = []
    mismatched_amounts = 0
    remaining = list(findings)

    for expectation in list(expectations):
        hit = next((f for f in remaining if _matches(expectation, f)), None)
        if hit is None:
            continue

        remaining.remove(hit)
        expectations.remove(expectation)
        matched.append(expectation.risk_type)

        if hit.amount_at_risk != expectation.amount_at_risk:
            mismatched_amounts += 1

    return ScenarioEvaluation(
        scenario_id=scenario,
        seed=seed,
        true_positives=len(matched),
        false_positives=len(remaining),
        false_negatives=len(expectations),
        amount_mismatches=mismatched_amounts,
        matched=tuple(sorted(matched)),
        unmatched_expected=tuple(sorted(e.risk_type for e in expectations)),
        unmatched_detected=tuple(sorted(f.risk_type for f in remaining)),
    )


def evaluate_all(scenarios: tuple[str, ...], seed: int = SEED) -> AggregateEvaluation:
    return AggregateEvaluation(
        scenarios=tuple(evaluate_scenario(scenario, seed) for scenario in scenarios)
    )


# -- regression fixtures --------------------------------------------------


def detection_snapshot(scenario: str, seed: int = SEED) -> dict[str, Any]:
    """A canonical, comparable record of what detection produced.

    Committed to `fixtures/` so that a behaviour change fails loudly instead of
    drifting unnoticed. Deliberately excludes ground truth: this is a record of
    the *output*, not of the answer.
    """
    findings = detect_for_scenario(scenario, seed)

    return {
        "scenario_id": scenario,
        "seed": seed,
        "as_of": AS_OF.isoformat().replace("+00:00", "Z"),
        "findings": [
            {
                "risk_type": finding.risk_type,
                "order_ref": finding.order_ref,
                "amount_at_risk_minor": finding.amount_at_risk,
                "currency": finding.currency,
                "confidence_bps": finding.confidence_bps,
                "detection_rule": finding.detection_rule,
                "reason": finding.reason,
                "evidence_event_ids": list(finding.evidence_event_ids),
            }
            for finding in findings
        ],
    }


def fixture_path(scenario: str, seed: int = SEED) -> Path:
    return FIXTURE_DIR / f"{scenario}_seed{seed}.json"


def write_fixture(scenario: str, seed: int = SEED) -> Path:
    """Regenerate one committed fixture. Run deliberately, never from a test."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = fixture_path(scenario, seed)
    path.write_text(
        json.dumps(detection_snapshot(scenario, seed), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def read_fixture(scenario: str, seed: int = SEED) -> dict[str, Any]:
    return json.loads(fixture_path(scenario, seed).read_text(encoding="utf-8"))
