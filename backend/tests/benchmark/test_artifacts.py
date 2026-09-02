"""The committed artifacts, and the boundary between them.

Three files, two kinds, one rule each:

* `docs/evaluation.json` — the causal report. Estimates, intervals, the ledger,
  the uplift model, quadrant counts.
* `frontend/src/fixtures/report.10k.json` — a **byte-for-byte copy** of it.
  `frontend/src/fixtures/index.ts` says so in as many words, and the app reads
  the fixture at build time with no API layer, so a copy that drifts turns the
  dashboard into a picture of a run that no longer exists.
* `docs/gate_comparison.json` — the gate and spend comparison. Decisions, not
  estimates.

**The separation is the point, and it is asserted in both directions.** The
causal artifact must not grow gate metrics, and the gate artifact must not
restate causal ones. `run_acceptance.py` records the reason: a spend comparison
is not a causal result, and merging them would let a policy change look like a
change in measured effect.

These tests read the committed files and touch no database. They are cheap, and
they are the only thing standing between a regenerated report and a fixture
that quietly kept yesterday's numbers — the exact drift that went unnoticed
from Day 1.2 until DR-4.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
EVALUATION_JSON = REPO_ROOT / "docs" / "evaluation.json"
FIXTURE_JSON = REPO_ROOT / "frontend" / "src" / "fixtures" / "report.10k.json"
GATE_JSON = REPO_ROOT / "docs" / "gate_comparison.json"

#: The measured post-DR-4 gate result on seed=42 N=10,000. Written out rather
#: than recomputed: a test that recomputed them would pass whatever the gate
#: currently does, which is precisely the property being pinned.
EXPECTED_GATE = {
    "treatment_units": 5_044,
    "gate_off_acted": 5_044,
    "gate_on_acted": 2_450,
    "gate_on_abstained": 2_594,
    "gate_on_explored": 87,
    "gate_on_ordinary": 2_363,
    "gray_zone_total": 1_879,
    "gray_zone_ordinary": 0,
    "gray_zone_explored": 87,
    "gray_zone_abstained": 1_792,
    "self_recovery_likely": 1_674,
    "uplift_not_significant": 118,
    "action_reduction": 1_674,
    "action_reduction_pct": 40.59,
    "cost_avoided": 5_188.0,
}

#: Present in the causal artifact and nowhere else.
CAUSAL_ONLY = ("recovery", "ledger", "accuracy", "harm", "power", "balance", "ground_truth")

#: Words that would mean a gate decision leaked into the causal artifact.
GATE_MARKERS = ("gate_on", "gate_off", "abstain", "self_recovery_likely", "explored")


def _bytes(path: pathlib.Path) -> bytes:
    if not path.exists():
        pytest.fail(f"{path} is missing; regenerate the artifacts")
    return path.read_bytes()


def _json(path: pathlib.Path) -> dict:
    return json.loads(_bytes(path))


class TestTheFixtureMatchesTheReport:
    def test_they_are_byte_identical(self) -> None:
        """The documented contract, asserted as written.

        Byte equality rather than semantic: `index.ts` promises a byte-for-byte
        copy, the file is generated rather than authored, and a weaker check
        would pass on a fixture whose keys were reordered by a hand edit — which
        is one of the ways a hand edit announces itself.
        """
        report, fixture = _bytes(EVALUATION_JSON), _bytes(FIXTURE_JSON)
        assert hashlib.sha256(report).hexdigest() == hashlib.sha256(fixture).hexdigest(), (
            f"{FIXTURE_JSON.name} has drifted from {EVALUATION_JSON.name} "
            f"({len(report)} vs {len(fixture)} bytes). Copy the report over the "
            f"fixture; do not hand-edit either."
        )

    def test_the_divergent_keys_are_named_when_they_differ(self) -> None:
        """A second, weaker assertion that exists for its failure message.

        Byte equality answers *whether* they diverged; this answers *where*,
        which is the question anyone reading the failure will ask next.
        """
        report, fixture = _json(EVALUATION_JSON), _json(FIXTURE_JSON)
        divergent = sorted(
            key for key in set(report) | set(fixture) if report.get(key) != fixture.get(key)
        )
        assert not divergent, f"keys differing between report and fixture: {divergent}"

    def test_the_shared_causal_fields_are_present_in_both(self) -> None:
        report, fixture = _json(EVALUATION_JSON), _json(FIXTURE_JSON)
        for key in CAUSAL_ONLY:
            assert key in report, f"{key} missing from {EVALUATION_JSON.name}"
            assert key in fixture, f"{key} missing from {FIXTURE_JSON.name}"

    def test_both_declare_themselves_synthetic(self) -> None:
        for path in (EVALUATION_JSON, FIXTURE_JSON):
            assert "SYNTHETIC" in _json(path)["label"], path


class TestTheGateArtifactIsSeparate:
    def test_the_causal_artifact_carries_no_gate_metric(self) -> None:
        """Deliberate, not an omission. `run_acceptance.py` says why.

        A gate metric appearing here would let a policy change read as a change
        in measured effect, which is the one confusion this project cannot
        afford.
        """
        rendered = _bytes(EVALUATION_JSON).decode("utf-8")
        present = [marker for marker in GATE_MARKERS if marker in rendered]
        assert not present, f"gate metrics leaked into the causal artifact: {present}"

    def test_the_gate_artifact_restates_no_causal_estimate(self) -> None:
        gate = _json(GATE_JSON)
        for key in CAUSAL_ONLY:
            assert key not in gate, f"{key} does not belong in {GATE_JSON.name}"

    def test_both_name_the_same_population(self) -> None:
        """The only thing the two artifacts may share: which run they describe."""
        report, gate = _json(EVALUATION_JSON), _json(GATE_JSON)
        assert gate["experiment_id"] == report["experiment"]["id"]
        assert gate["case_count"] == report["ground_truth"]["n"]


class TestTheGateTotals:
    @pytest.fixture
    def gate(self) -> dict:
        return _json(GATE_JSON)

    def test_the_headline_counts_are_the_measured_ones(self, gate: dict) -> None:
        assert gate["treatment_units"] == EXPECTED_GATE["treatment_units"]
        assert gate["gate_off"]["acted"] == EXPECTED_GATE["gate_off_acted"]
        assert gate["gate_off"]["abstained"] == 0
        assert gate["gate_on"]["acted"] == EXPECTED_GATE["gate_on_acted"]
        assert gate["gate_on"]["abstained"] == EXPECTED_GATE["gate_on_abstained"]
        assert gate["gate_on"]["explored"] == EXPECTED_GATE["gate_on_explored"]
        assert gate["gate_on"]["ordinary_acted"] == EXPECTED_GATE["gate_on_ordinary"]

    def test_the_gray_zone_never_acts_ordinarily(self, gate: dict) -> None:
        """DR-4's whole purpose, pinned in the artifact as well as the gate."""
        assert gate["gray_zone"]["ordinary_acted"] == EXPECTED_GATE["gray_zone_ordinary"]
        assert gate["by_quadrant"]["gray_zone"]["ordinary_acted"] == 0

    def test_the_gray_zone_breakdown_is_the_measured_one(self, gate: dict) -> None:
        gz = gate["gray_zone"]
        assert gz["total"] == EXPECTED_GATE["gray_zone_total"]
        assert gz["explored"] == EXPECTED_GATE["gray_zone_explored"]
        assert gz["abstained"] == EXPECTED_GATE["gray_zone_abstained"]
        assert gz["self_recovery_likely"] == EXPECTED_GATE["self_recovery_likely"]
        assert gz["uplift_not_significant"] == EXPECTED_GATE["uplift_not_significant"]

    def test_the_reduction_against_the_accepted_baseline(self, gate: dict) -> None:
        assert gate["baseline"]["acted"] == 4_124
        assert gate["baseline"]["abstained"] == 920
        assert gate["action_reduction"] == EXPECTED_GATE["action_reduction"]
        assert gate["action_reduction_pct"] == EXPECTED_GATE["action_reduction_pct"]
        assert gate["cost_avoided"] == EXPECTED_GATE["cost_avoided"]
        assert gate["cost_avoided_minor"] == 518_800

    def test_the_money_is_authoritative_in_minor_units(self, gate: dict) -> None:
        """Integers decide; the rupee figure is derived for display only."""
        assert isinstance(gate["cost_avoided_minor"], int)
        assert gate["cost_avoided_minor"] == round(gate["cost_avoided"] * 100)
        assert (
            gate["gate_on_cost_minor"] + gate["cost_avoided_minor"] == gate["gate_off_cost_minor"]
        )

    def test_the_counts_reconcile_to_the_treatment_population(self, gate: dict) -> None:
        n = gate["treatment_units"]
        on = gate["gate_on"]
        assert on["acted"] + on["abstained"] == n
        assert on["ordinary_acted"] + on["explored"] == on["acted"]
        assert sum(q["total"] for q in gate["by_quadrant"].values()) == n
        assert sum(gate["abstention_reasons"].values()) == on["abstained"]

    def test_it_carries_the_provenance_needed_to_reproduce_it(self, gate: dict) -> None:
        """A number nobody can regenerate is an assertion, not a measurement."""
        p = gate["provenance"]
        assert p["as_of"].startswith("2026-08-31")
        assert p["gray_zone_policy"] == "current_baseline"
        assert p["folds"] == 5
        assert p["resamples"] == 10_000
        assert gate["seed"] == 42
        assert gate["exploration_budget_bps"] == 500
        assert gate["unit_cost"] == 200
        assert gate["intervention"] == "create_payment_link"

    def test_it_declares_itself_synthetic(self, gate: dict) -> None:
        assert "SYNTHETIC" in gate["label"]
