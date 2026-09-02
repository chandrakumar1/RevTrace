"""The deterministic evaluator, tested exhaustively and without a database.

The whole point of this layer is that it is not the model: given statistics and
a claim, the verdict is arithmetic. So the tests enumerate the rule table rather
than sampling it — every claim crossed with every branch — and assert that the
same inputs always produce the same result.

Nothing here needs a session, a clock, or a network. Nothing here reads a
`truth_*` column, and a guard below proves the module cannot.
"""

from __future__ import annotations

import ast
import itertools
import json
import pathlib
import uuid

import pytest

from app.agents.contracts import (
    EXPLORATORY_NOTE,
    CellStat,
    Claim,
    PopulationSummary,
    Status,
    ValidatedHypothesis,
)
from app.engine.falsification import (
    RULE_ABOVE_POPULATION,
    RULE_BELOW_POPULATION,
    RULE_INTERVAL_SPANS_POPULATION,
    RULE_INTERVAL_SPANS_ZERO,
    RULE_NOT_QUALIFIED,
    RULE_NULL_CLAIM_REFUTED,
    FalsificationError,
    compare_to_population,
    falsify,
)

MODULE = pathlib.Path("app/engine/falsification.py")
EXPERIMENT = uuid.UUID("22222222-2222-4222-8222-222222222222")
POPULATION_ATE = 1_564


def a_population(ate_bps: int = POPULATION_ATE) -> PopulationSummary:
    return PopulationSummary(
        ate_bps=ate_bps,
        ci_low_bps=1_370,
        ci_high_bps=1_757,
        n_treatment=5_044,
        n_holdout=4_956,
        feature_vocabulary=("failure_code", "payment_method"),
    )


def a_cell(
    *,
    key: str = "insufficient_funds|upi",
    level: str = "fine",
    uplift: int,
    low: int,
    high: int,
    qualified: bool = True,
    reason: str = "qualified",
) -> CellStat:
    return CellStat(
        cell_key=key,
        ladder_level=level,
        n_treated=4_000,
        n_holdout=4_000,
        recovered_treated=1_180,
        recovered_holdout=640,
        p_treat_bps=2_950,
        p_control_bps=1_600,
        uplift_bps=uplift,
        ci_low_bps=low,
        ci_high_bps=high,
        qualified=qualified,
        qualification_reason=reason,
    )


def a_hypothesis(claim: Claim, *, key: str = "insufficient_funds|upi") -> ValidatedHypothesis:
    return ValidatedHypothesis(
        hypothesis_id=uuid.UUID(int=1),
        experiment_id=EXPERIMENT,
        cell_key=key,
        ladder_level="fine",
        claim=claim,
        rationale="from the counts shown",
        evidence_cited=(key,),
    )


#: Interval lies entirely ABOVE the population effect (1564).
ABOVE = {"uplift": 2_500, "low": 2_000, "high": 3_000}
#: Interval lies entirely BELOW the population effect, still clear of zero.
BELOW = {"uplift": 500, "low": 200, "high": 800}
#: Interval CONTAINS the population effect — looks higher, is not shown to be.
SPANS_ATE = {"uplift": 1_875, "low": 1_507, "high": 2_245}
#: Interval spans zero.
NULL = {"uplift": 50, "low": -300, "high": 400}


# -- rule 1: qualification --------------------------------------------------


class TestQualificationComesFirst:
    @pytest.mark.parametrize("claim", list(Claim))
    @pytest.mark.parametrize("shape", [ABOVE, BELOW, NULL], ids=["above", "below", "null"])
    def test_an_unqualified_cell_is_always_insufficient(
        self, claim: Claim, shape: dict[str, int]
    ) -> None:
        """No claim and no interval can rescue a cell that never qualified."""
        result = falsify(
            a_hypothesis(claim),
            a_cell(**shape, qualified=False, reason="underpowered"),
            a_population(),
        )
        assert result.status is Status.INSUFFICIENT_EVIDENCE
        assert result.rule == RULE_NOT_QUALIFIED

    def test_the_reason_names_the_qualification_failure(self) -> None:
        result = falsify(
            a_hypothesis(Claim.HIGHER),
            a_cell(**ABOVE, qualified=False, reason="empty_arm"),
            a_population(),
        )
        assert "empty_arm" in result.reason


# -- rule 2: a null interval settles nothing --------------------------------


class TestIntervalContainingZero:
    def test_no_effect_is_decided_against_zero(self) -> None:
        """Only `no_effect` consults zero. It is a claim about zero."""
        result = falsify(a_hypothesis(Claim.NO_EFFECT), a_cell(**NULL), a_population())
        assert result.status is Status.INSUFFICIENT_EVIDENCE
        assert result.rule == RULE_INTERVAL_SPANS_ZERO

    @pytest.mark.parametrize("claim", [Claim.HIGHER, Claim.LOWER])
    def test_the_comparative_claims_never_consult_zero(self, claim: Claim) -> None:
        """The boundary that must not blur.

        `NULL` spans zero but lies entirely *below* the population effect, so a
        comparative claim is decided — against the ATE — rather than deferred.
        If the zero rule ever leaked into these claims this would fail.
        """
        result = falsify(a_hypothesis(claim), a_cell(**NULL), a_population())
        assert result.rule != RULE_INTERVAL_SPANS_ZERO
        assert result.rule == RULE_BELOW_POPULATION
        assert result.status is (Status.CONFIRMED if claim is Claim.LOWER else Status.REFUTED)

    def test_no_effect_is_never_confirmed_by_a_null_result(self) -> None:
        """Absence of evidence is not evidence of absence.

        Confirming `no_effect` here would be the overclaim the uplift
        limitations already refuse: separating "no effect" from "an effect too
        small to matter" needs equivalence testing against a margin.
        """
        result = falsify(a_hypothesis(Claim.NO_EFFECT), a_cell(**NULL), a_population())
        assert result.status is not Status.CONFIRMED
        assert result.status is Status.INSUFFICIENT_EVIDENCE
        assert "not evidence that none exists" in result.reason

    @pytest.mark.parametrize(
        ("low", "high"),
        [(0, 500), (-500, 0), (0, 0), (-1, 1)],
        ids=["zero-is-low", "zero-is-high", "degenerate", "straddling"],
    )
    def test_the_bounds_are_inclusive(self, low: int, high: int) -> None:
        """A bound exactly on zero counts as containing it.

        Probed through `no_effect`, the only claim decided against zero.
        """
        result = falsify(
            a_hypothesis(Claim.NO_EFFECT),
            a_cell(uplift=(low + high) // 2, low=low, high=high),
            a_population(),
        )
        assert result.rule == RULE_INTERVAL_SPANS_ZERO
        assert result.status is Status.INSUFFICIENT_EVIDENCE


# -- rules 3 and 4: direction ----------------------------------------------


class TestDirection:
    def test_higher_claim_above_the_population_is_confirmed(self) -> None:
        result = falsify(a_hypothesis(Claim.HIGHER), a_cell(**ABOVE), a_population())
        assert result.status is Status.CONFIRMED
        assert result.rule == RULE_ABOVE_POPULATION

    def test_higher_claim_below_the_population_is_refuted(self) -> None:
        result = falsify(a_hypothesis(Claim.HIGHER), a_cell(**BELOW), a_population())
        assert result.status is Status.REFUTED
        assert result.rule == RULE_BELOW_POPULATION

    def test_lower_claim_below_the_population_is_confirmed(self) -> None:
        result = falsify(a_hypothesis(Claim.LOWER), a_cell(**BELOW), a_population())
        assert result.status is Status.CONFIRMED
        assert result.rule == RULE_BELOW_POPULATION

    def test_lower_claim_above_the_population_is_refuted(self) -> None:
        result = falsify(a_hypothesis(Claim.LOWER), a_cell(**ABOVE), a_population())
        assert result.status is Status.REFUTED
        assert result.rule == RULE_ABOVE_POPULATION

    def test_no_effect_with_a_detected_effect_is_refuted(self) -> None:
        for shape in (ABOVE, BELOW):
            result = falsify(a_hypothesis(Claim.NO_EFFECT), a_cell(**shape), a_population())
            assert result.status is Status.REFUTED
            assert result.rule == RULE_NULL_CLAIM_REFUTED

    def test_an_interval_bracketing_the_population_settles_nothing(self) -> None:
        """The bug this rule exists to fix.

        The point estimate sits on the population effect and the interval
        straddles it, so neither direction is established. The earlier rule
        refuted both claims here; that was an overclaim in the opposite
        direction from the false confirmation.
        """
        at_par = a_cell(uplift=POPULATION_ATE, low=POPULATION_ATE - 100, high=POPULATION_ATE + 100)
        for claim in (Claim.HIGHER, Claim.LOWER):
            result = falsify(a_hypothesis(claim), at_par, a_population())
            assert result.status is Status.INSUFFICIENT_EVIDENCE
            assert result.rule == RULE_INTERVAL_SPANS_POPULATION

    def test_one_basis_point_flips_the_verdict(self) -> None:
        """The boundary is exact, because the comparison is integer.

        The bound moves, not the point estimate: `ci_low` one bps above the
        population effect confirms, one bps below leaves the interval touching
        it and settles nothing.
        """
        clear = a_cell(uplift=2_500, low=POPULATION_ATE + 1, high=3_000)
        touching = a_cell(uplift=2_500, low=POPULATION_ATE - 1, high=3_000)
        assert falsify(a_hypothesis(Claim.HIGHER), clear, a_population()).status is Status.CONFIRMED
        assert (
            falsify(a_hypothesis(Claim.HIGHER), touching, a_population()).status
            is Status.INSUFFICIENT_EVIDENCE
        )

    def test_the_comparison_is_strict_at_the_bound(self) -> None:
        """`ci_low == ate` does not confirm: the rule is `>`, not `>=`."""
        equal_low = a_cell(uplift=2_500, low=POPULATION_ATE, high=3_000)
        equal_high = a_cell(uplift=500, low=200, high=POPULATION_ATE)
        higher = falsify(a_hypothesis(Claim.HIGHER), equal_low, a_population())
        lower = falsify(a_hypothesis(Claim.LOWER), equal_high, a_population())
        assert higher.status is Status.INSUFFICIENT_EVIDENCE
        assert lower.status is Status.INSUFFICIENT_EVIDENCE

    @pytest.mark.parametrize(
        ("claim", "low", "high", "status", "rule"),
        [
            # higher: the whole interval decides, never the point estimate.
            (Claim.HIGHER, 2_000, 3_000, Status.CONFIRMED, RULE_ABOVE_POPULATION),
            (Claim.HIGHER, 200, 800, Status.REFUTED, RULE_BELOW_POPULATION),
            (
                Claim.HIGHER,
                1_507,
                2_245,
                Status.INSUFFICIENT_EVIDENCE,
                RULE_INTERVAL_SPANS_POPULATION,
            ),
            # lower: mirrored.
            (Claim.LOWER, 200, 800, Status.CONFIRMED, RULE_BELOW_POPULATION),
            (Claim.LOWER, 2_000, 3_000, Status.REFUTED, RULE_ABOVE_POPULATION),
            (
                Claim.LOWER,
                1_507,
                2_245,
                Status.INSUFFICIENT_EVIDENCE,
                RULE_INTERVAL_SPANS_POPULATION,
            ),
        ],
    )
    def test_compare_to_population_is_exhaustive(
        self, claim: Claim, low: int, high: int, status: Status, rule: str
    ) -> None:
        assert compare_to_population(claim, low, high, POPULATION_ATE) == (status, rule)

    def test_compare_to_population_refuses_the_null_claim(self) -> None:
        """`no_effect` must never reach the ATE comparison at all."""
        with pytest.raises(FalsificationError, match="not a comparative claim"):
            compare_to_population(Claim.NO_EFFECT, 2_000, 3_000, POPULATION_ATE)


class TestTheFullRuleTable:
    """Every claim crossed with every branch. Nothing falls through."""

    @pytest.mark.parametrize("claim", list(Claim))
    @pytest.mark.parametrize("qualified", [True, False])
    @pytest.mark.parametrize(
        "shape",
        [ABOVE, BELOW, SPANS_ATE, NULL],
        ids=["above", "below", "spans-ate", "null"],
    )
    def test_every_combination_reaches_exactly_one_known_rule(
        self, claim: Claim, qualified: bool, shape: dict[str, int]
    ) -> None:
        result = falsify(a_hypothesis(claim), a_cell(**shape, qualified=qualified), a_population())
        assert result.status in set(Status)
        assert result.rule in {
            RULE_NOT_QUALIFIED,
            RULE_INTERVAL_SPANS_ZERO,
            RULE_NULL_CLAIM_REFUTED,
            RULE_ABOVE_POPULATION,
            RULE_BELOW_POPULATION,
            RULE_INTERVAL_SPANS_POPULATION,
        }
        assert result.reason.strip()

    def test_no_effect_is_never_confirmed_under_any_combination(self) -> None:
        for qualified, shape in itertools.product((True, False), (ABOVE, BELOW, SPANS_ATE, NULL)):
            result = falsify(
                a_hypothesis(Claim.NO_EFFECT),
                a_cell(**shape, qualified=qualified),
                a_population(),
            )
            assert result.status is not Status.CONFIRMED

    def test_the_two_rule_families_never_cross_contaminate(self) -> None:
        """The semantic boundary, asserted directly.

        `no_effect` may only ever be decided by a zero-based rule; `higher` and
        `lower` may only ever be decided by an ATE-based one. Qualification is
        the single rule both families share.
        """
        zero_rules = {RULE_INTERVAL_SPANS_ZERO, RULE_NULL_CLAIM_REFUTED}
        ate_rules = {
            RULE_ABOVE_POPULATION,
            RULE_BELOW_POPULATION,
            RULE_INTERVAL_SPANS_POPULATION,
        }
        for qualified, shape in itertools.product((True, False), (ABOVE, BELOW, SPANS_ATE, NULL)):
            cell = a_cell(**shape, qualified=qualified)

            null_result = falsify(a_hypothesis(Claim.NO_EFFECT), cell, a_population())
            assert null_result.rule not in ate_rules, null_result.rule
            assert null_result.rule in zero_rules | {RULE_NOT_QUALIFIED}

            for claim in (Claim.HIGHER, Claim.LOWER):
                comparative = falsify(a_hypothesis(claim), cell, a_population())
                assert comparative.rule not in zero_rules, comparative.rule
                assert comparative.rule in ate_rules | {RULE_NOT_QUALIFIED}

    def test_the_population_ate_never_decides_the_null_claim(self) -> None:
        """Moving the ATE cannot change a `no_effect` verdict."""
        cell = a_cell(**ABOVE)
        verdicts = {
            falsify(a_hypothesis(Claim.NO_EFFECT), cell, a_population(ate_bps=ate)).status
            for ate in (-9_000, 0, 1_564, 2_500, 9_000)
        }
        assert verdicts == {Status.REFUTED}

    def test_zero_never_decides_a_comparative_claim(self) -> None:
        """Two cells with identical position relative to the ATE but opposite
        relationships to zero must receive the same verdict."""
        population = a_population(ate_bps=0)
        spans_zero = falsify(
            a_hypothesis(Claim.HIGHER), a_cell(uplift=250, low=100, high=400), population
        )
        # Same interval, still entirely above an ATE of 0 — and clear of zero.
        clear_of_zero = falsify(
            a_hypothesis(Claim.HIGHER), a_cell(uplift=250, low=1, high=400), population
        )
        assert spans_zero.status is clear_of_zero.status is Status.CONFIRMED
        assert spans_zero.rule == clear_of_zero.rule == RULE_ABOVE_POPULATION


# -- integers, determinism, and honesty -------------------------------------


class TestIntegersOnly:
    def test_every_evidence_value_is_an_integer(self) -> None:
        result = falsify(a_hypothesis(Claim.HIGHER), a_cell(**ABOVE), a_population())
        for name, value in result.evidence:
            assert isinstance(value, int), name
            assert not isinstance(value, bool), name

    def test_the_payload_contains_no_float(self) -> None:
        payload = falsify(a_hypothesis(Claim.HIGHER), a_cell(**ABOVE), a_population()).as_dict()
        rendered = json.loads(json.dumps(payload))
        for key, value in rendered["evidence"].items():
            assert isinstance(value, int), key
        for key, value in rendered.items():
            assert not isinstance(value, float), key

    def test_the_module_forms_no_float(self) -> None:
        """No division, no float literal, no float() call anywhere."""
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), f"float literal {node.value}"
            if isinstance(node, ast.BinOp):
                assert not isinstance(node.op, ast.Div), "true division forms a float"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "float", "float() call"

    def test_the_evidence_carries_the_population_effect(self) -> None:
        result = falsify(a_hypothesis(Claim.HIGHER), a_cell(**ABOVE), a_population())
        assert dict(result.evidence)["population_ate_bps"] == POPULATION_ATE


class TestDeterminism:
    def test_repeated_evaluation_is_identical(self) -> None:
        hypothesis, cell, population = a_hypothesis(Claim.HIGHER), a_cell(**ABOVE), a_population()
        results = {falsify(hypothesis, cell, population) for _ in range(100)}
        assert len(results) == 1

    def test_the_payload_is_stable(self) -> None:
        args = (a_hypothesis(Claim.LOWER), a_cell(**BELOW), a_population())
        assert falsify(*args).as_dict() == falsify(*args).as_dict()

    def test_the_module_reads_no_clock_and_draws_nothing(self) -> None:
        banned = {"now", "utcnow", "today", "uuid4", "random", "choice", "shuffle"}
        found: set[str] = set()
        for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.Name):
                found.add(node.id)
        assert found & banned == set(), found & banned


class TestExploratoryLabelling:
    def test_every_result_is_marked_exploratory(self) -> None:
        for claim, shape in itertools.product(list(Claim), (ABOVE, BELOW, NULL)):
            result = falsify(a_hypothesis(claim), a_cell(**shape), a_population())
            assert result.exploratory is True
            assert result.as_dict()["exploratory"] is True

    def test_the_note_travels_with_the_status(self) -> None:
        payload = falsify(a_hypothesis(Claim.HIGHER), a_cell(**ABOVE), a_population()).as_dict()
        assert payload["note"] == EXPLORATORY_NOTE
        note = str(payload["note"])
        assert "not a pre-registered confirmatory result" in note
        assert "no multiplicity correction has been applied" in note
        assert "circular" in note

    def test_no_result_is_ever_called_confirmatory(self) -> None:
        for claim in Claim:
            payload = falsify(a_hypothesis(claim), a_cell(**ABOVE), a_population()).as_dict()
            rendered = json.dumps(payload).lower()
            assert "pre-registered confirmatory" not in rendered.replace(
                "not a pre-registered confirmatory result", ""
            )


class TestGuards:
    def test_a_mismatched_cell_is_refused(self) -> None:
        """The evaluator will not silently test a different cell."""
        with pytest.raises(FalsificationError, match="but was given statistics"):
            falsify(
                a_hypothesis(Claim.HIGHER, key="insufficient_funds|upi"),
                a_cell(key="card_declined|card", **ABOVE),
                a_population(),
            )

    def test_it_names_no_truth_column(self) -> None:
        truth = {"truth_y0", "truth_y1", "truth_harm_0", "truth_harm_1", "truth_segment"}
        found: set[str] = set()
        for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
        assert found & truth == set(), found & truth

    def test_it_imports_no_estimator_reporter_or_session(self) -> None:
        """It consumes already-computed statistics; it recomputes nothing."""
        modules: list[str] = []
        for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        for module in modules:
            assert not module.startswith("app.causal"), module
            assert "evaluation" not in module, module
            assert "reporting" not in module, module
            assert "sqlalchemy" not in module, module
            assert "policy_engine" not in module, module

    def test_it_calls_no_bootstrap_or_estimator(self) -> None:
        banned = {"bootstrap_interval", "assign_quadrants", "load_units", "load_population", "rank"}
        called: set[str] = set()
        for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
        assert called & banned == set(), called & banned


class TestRealPopulationRegression:
    """The two cells that exposed the bug, with their real measured statistics.

    Both are qualified cells from the accepted seed=42 N=10,000 run, derived
    from the materialised population against a population effect of 1564 bps.
    They are here because seven of the nine qualified cells agree under either
    rule — the defect only surfaces where a cell's interval brackets the
    population effect, so a regression test needs exactly these numbers.
    """

    #: The accepted run's measured population effect.
    REAL_ATE = 1_564

    def a_real_population(self) -> PopulationSummary:
        return PopulationSummary(
            ate_bps=self.REAL_ATE,
            ci_low_bps=1_370,
            ci_high_bps=1_757,
            n_treatment=5_044,
            n_holdout=4_956,
            feature_vocabulary=("failure_code", "payment_method"),
        )

    def test_insufficient_funds_coarse_is_not_confirmed(self) -> None:
        """`insufficient_funds` coarse: uplift 1875, CI [1507, 2245].

        The interval contains 1564. The point estimate is higher; the evidence
        does not establish that it is. The earlier rule confirmed this.
        """
        cell = a_cell(
            key="insufficient_funds",
            level="coarse",
            uplift=1_875,
            low=1_507,
            high=2_245,
        )
        result = falsify(
            a_hypothesis(Claim.HIGHER, key="insufficient_funds"),
            cell,
            self.a_real_population(),
        )
        assert result.status is Status.INSUFFICIENT_EVIDENCE
        assert result.rule == RULE_INTERVAL_SPANS_POPULATION
        assert result.status is not Status.CONFIRMED

    def test_mandate_inactive_upi_is_refuted(self) -> None:
        """`mandate_inactive|upi`: uplift 14, CI [-571, 597].

        Spans zero, so the earlier rule deferred. But the whole interval lies
        below 1564, which does establish the cell is not higher.
        """
        cell = a_cell(key="mandate_inactive|upi", uplift=14, low=-571, high=597)
        result = falsify(
            a_hypothesis(Claim.HIGHER, key="mandate_inactive|upi"),
            cell,
            self.a_real_population(),
        )
        assert result.status is Status.REFUTED
        assert result.rule == RULE_BELOW_POPULATION

    def test_the_same_cell_confirms_the_lower_claim(self) -> None:
        cell = a_cell(key="mandate_inactive|upi", uplift=14, low=-571, high=597)
        result = falsify(
            a_hypothesis(Claim.LOWER, key="mandate_inactive|upi"),
            cell,
            self.a_real_population(),
        )
        assert result.status is Status.CONFIRMED
        assert result.rule == RULE_BELOW_POPULATION

    def test_card_declined_card_remains_confirmed(self) -> None:
        """A cell both rules agree on, so the fix is not over-correcting."""
        cell = a_cell(key="card_declined|card", uplift=3_373, low=3_006, high=3_738)
        result = falsify(
            a_hypothesis(Claim.HIGHER, key="card_declined|card"),
            cell,
            self.a_real_population(),
        )
        assert result.status is Status.CONFIRMED
        assert result.rule == RULE_ABOVE_POPULATION

    def test_no_effect_on_the_null_cell_stays_insufficient(self) -> None:
        """`mandate_inactive|upi` spans zero, so the null claim is undecidable."""
        cell = a_cell(key="mandate_inactive|upi", uplift=14, low=-571, high=597)
        result = falsify(
            a_hypothesis(Claim.NO_EFFECT, key="mandate_inactive|upi"),
            cell,
            self.a_real_population(),
        )
        assert result.status is Status.INSUFFICIENT_EVIDENCE
        assert result.rule == RULE_INTERVAL_SPANS_ZERO
