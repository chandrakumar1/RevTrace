"""The gate-off/gate-on comparison: its arithmetic, and its shape on real data.

Split the same way `test_coverage.py` is. The arithmetic is pure and is tested
against hand-built comparisons with no database — that is the part a regression
would silently corrupt. The *shape* is tested against a real population at a
size the fast suite can afford; the N=10,000 run is a deliberate exercise, not
something every test session pays for.

Nothing here asserts a particular avoidance rate. A test that required the gate
to decline some fraction would be tuning the gate to a conclusion, and the
comparison exists to measure whatever is true.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.causal.estimators import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED
from app.engine.policy_engine import (
    EXPLORATION_BUDGET_BPS,
    GrayZonePolicy,
    UpliftEvidence,
)
from app.models.enums import AbstainReason, Arm, CaseDecision, Quadrant
from app.models.experiment_result import ExperimentResult
from app.reporting.evaluation import build_report
from tests.benchmark.bridge import BENCHMARK_ACTION, materialise
from tests.benchmark.gate_benchmark import (
    BPS_SCALE,
    BUDGET_BPS_GRID,
    HONESTY,
    PERCENTILES,
    POLICY_GRID,
    SCENARIO_GRID,
    WATCHED_TABLES,
    AmountDistribution,
    CausalSnapshot,
    GateBenchmarkError,
    GateComparison,
    Scenario,
    ScenarioResult,
    SensitivityRun,
    _percentile,
    capture_snapshot,
    report_digest,
    row_counts,
    run_gate_comparison,
    run_sensitivity,
    summarise,
    summarise_sensitivity,
    tally_abstentions,
)

#: Small enough for the fast suite; the comparison's behaviour does not depend
#: on it. Matched to the sibling uplift test so the model actually fits.
SMALL_CASE_COUNT = 400
FAST_RESAMPLES = 40
SMALL_SEED = 91

AS_OF = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def a_comparison(
    *,
    n_treatment: int = 1_000,
    gate_on_actions: int = 600,
    unit_cost: int = 200,
    abstentions: dict[str, int] | None = None,
    explored: int = 0,
) -> GateComparison:
    """A hand-built comparison. Abstentions default to filling the remainder."""
    if abstentions is None:
        abstentions = {AbstainReason.SURE_THING.value: n_treatment - gate_on_actions}
    counts = dict.fromkeys((reason.value for reason in AbstainReason), 0)
    counts.update(abstentions)
    return GateComparison(
        n_treatment=n_treatment,
        gate_off_actions=n_treatment,
        gate_on_actions=gate_on_actions,
        intervention_code="create_payment_link",
        unit_cost=unit_cost,
        abstentions=tuple(counts.items()),
        explored=explored,
        exploration_budget_bps=EXPLORATION_BUDGET_BPS,
        ate_bps=1_564,
        incremental_recovered=447_880_605,
    )


class _Decision:
    """The two fields `tally_abstentions` reads."""

    def __init__(self, reason: AbstainReason | None) -> None:
        self.reason = reason
        self.decision = CaseDecision.ABSTAIN if reason else CaseDecision.ACT
        self.explored = False


# -- action arithmetic ----------------------------------------------------


class TestActionCounts:
    def test_gate_off_is_every_treatment_unit(self) -> None:
        comparison = a_comparison(n_treatment=5_044, gate_on_actions=3_000)
        assert comparison.gate_off_actions == 5_044

    def test_actions_avoided_is_the_difference(self) -> None:
        comparison = a_comparison(n_treatment=1_000, gate_on_actions=600)
        assert comparison.actions_avoided == 400

    def test_a_gate_that_declines_nothing_avoids_nothing(self) -> None:
        comparison = a_comparison(n_treatment=1_000, gate_on_actions=1_000, abstentions={})
        assert comparison.actions_avoided == 0
        assert comparison.cost_avoided == 0

    def test_a_gate_that_declines_everything_avoids_everything(self) -> None:
        comparison = a_comparison(n_treatment=1_000, gate_on_actions=0)
        assert comparison.actions_avoided == 1_000
        assert comparison.gate_on_cost == 0

    def test_acting_more_than_gate_off_is_refused(self) -> None:
        """The gate can only ever decline. More would be a counting bug."""
        with pytest.raises(GateBenchmarkError, match="only ever decline"):
            GateComparison(
                n_treatment=100,
                gate_off_actions=100,
                gate_on_actions=101,
                intervention_code="create_payment_link",
                unit_cost=200,
                abstentions=tally_abstentions([]),
                explored=0,
                exploration_budget_bps=EXPLORATION_BUDGET_BPS,
                ate_bps=0,
                incremental_recovered=0,
            )

    def test_every_unit_must_reach_exactly_one_decision(self) -> None:
        """Actions plus abstentions must account for the whole arm."""
        with pytest.raises(GateBenchmarkError, match="exactly one decision"):
            a_comparison(n_treatment=1_000, gate_on_actions=600, abstentions={})


# -- cost arithmetic ------------------------------------------------------


class TestCostArithmetic:
    def test_cost_avoided_is_actions_times_unit_cost(self) -> None:
        comparison = a_comparison(n_treatment=1_000, gate_on_actions=600, unit_cost=200)
        assert comparison.cost_avoided == 400 * 200

    def test_the_three_costs_reconcile(self) -> None:
        comparison = a_comparison(n_treatment=1_000, gate_on_actions=600)
        assert comparison.gate_on_cost + comparison.cost_avoided == comparison.gate_off_cost

    def test_a_free_intervention_avoids_no_money(self) -> None:
        """Declining a free action saves actions, not rupees."""
        comparison = a_comparison(n_treatment=1_000, gate_on_actions=0, unit_cost=0)
        assert comparison.actions_avoided == 1_000
        assert comparison.cost_avoided == 0

    def test_costs_are_exact_integers(self) -> None:
        comparison = a_comparison(n_treatment=5_044, gate_on_actions=1_337, unit_cost=200)
        for value in (comparison.cost_avoided, comparison.gate_on_cost, comparison.gate_off_cost):
            assert isinstance(value, int)
            assert not isinstance(value, bool)


class TestFractions:
    @pytest.mark.parametrize(
        ("gate_on", "total", "expected_bps"),
        [
            (600, 1_000, 6_000),
            (1_000, 1_000, 10_000),
            (0, 1_000, 0),
            (1, 3, 3_333),
            (2, 3, 6_667),
        ],
    )
    def test_the_gate_on_fraction_rounds_half_up(
        self, gate_on: int, total: int, expected_bps: int
    ) -> None:
        comparison = a_comparison(n_treatment=total, gate_on_actions=gate_on)
        assert comparison.gate_on_fraction_bps == expected_bps

    def test_the_two_fractions_sum_to_full_scale(self) -> None:
        comparison = a_comparison(n_treatment=1_000, gate_on_actions=600)
        assert comparison.gate_on_fraction_bps is not None
        assert comparison.avoided_fraction_bps is not None
        assert comparison.gate_on_fraction_bps + comparison.avoided_fraction_bps == BPS_SCALE

    def test_a_fraction_over_no_actions_is_undefined_not_zero(self) -> None:
        """Zero would read as "the gate declined everything"."""
        comparison = a_comparison(n_treatment=0, gate_on_actions=0, abstentions={})
        assert comparison.gate_on_fraction_bps is None
        assert comparison.avoided_fraction_bps is None
        assert comparison.explored_fraction_bps is None
        assert "undefined" in summarise(comparison)


# -- abstention reasons ---------------------------------------------------


class TestAbstentionReasons:
    def test_all_twelve_reasons_are_always_present(self) -> None:
        counts = dict(tally_abstentions([]))
        assert set(counts) == {reason.value for reason in AbstainReason}
        assert len(counts) == 12

    def test_a_reason_that_never_fired_is_retained_as_zero(self) -> None:
        """Absent and zero are different findings; both must be readable."""
        counts = dict(tally_abstentions([_Decision(AbstainReason.SURE_THING)]))
        assert counts[AbstainReason.SURE_THING.value] == 1
        assert counts[AbstainReason.REGULATORY_BLOCK.value] == 0
        assert AbstainReason.REGULATORY_BLOCK.value in counts

    def test_acting_decisions_are_not_counted(self) -> None:
        decisions = [_Decision(None), _Decision(None), _Decision(AbstainReason.LOST_CAUSE)]
        counts = dict(tally_abstentions(decisions))  # type: ignore[arg-type]
        assert sum(counts.values()) == 1

    def test_every_reason_counts_independently(self) -> None:
        decisions = [_Decision(reason) for reason in AbstainReason]
        counts = dict(tally_abstentions(decisions))  # type: ignore[arg-type]
        assert all(count == 1 for count in counts.values())

    def test_the_summary_lists_every_reason_including_zeroes(self) -> None:
        text = summarise(a_comparison())
        for reason in AbstainReason:
            assert reason.value in text

    def test_the_ordering_is_the_enum_ordering(self) -> None:
        """Deterministic, so two runs render identically."""
        first = tally_abstentions([])
        second = tally_abstentions([_Decision(AbstainReason.SURE_THING)])  # type: ignore[list-item]
        assert [name for name, _ in first] == [name for name, _ in second]
        assert [name for name, _ in first] == [reason.value for reason in AbstainReason]


# -- exploration ----------------------------------------------------------


class TestExploration:
    def test_the_budget_is_reported_untuned(self) -> None:
        assert a_comparison().exploration_budget_bps == EXPLORATION_BUDGET_BPS

    def test_the_explored_count_and_fraction(self) -> None:
        comparison = a_comparison(n_treatment=1_000, gate_on_actions=600, explored=50)
        assert comparison.explored == 50
        assert comparison.explored_fraction_bps == 500

    def test_zero_exploration_is_reported_as_zero_not_hidden(self) -> None:
        comparison = a_comparison(n_treatment=1_000, gate_on_actions=600, explored=0)
        assert comparison.explored_fraction_bps == 0
        assert "exploration selected" in summarise(comparison)


# -- shape and honesty ----------------------------------------------------


class TestReportedShape:
    def test_as_dict_carries_every_required_figure(self) -> None:
        payload = a_comparison().as_dict()
        assert set(payload) >= {
            "n_treatment",
            "gate_off_actions",
            "gate_on_actions",
            "actions_avoided",
            "unit_cost",
            "cost_avoided",
            "abstentions",
            "explored",
            "exploration_budget_bps",
            "ate_bps",
            "incremental_recovered",
            "gate_on_fraction_bps",
            "avoided_fraction_bps",
        }

    def test_no_float_appears_anywhere(self) -> None:
        payload = a_comparison().as_dict()
        for key, value in payload.items():
            assert not isinstance(value, float), key
        for reason, count in a_comparison().abstentions:
            assert isinstance(count, int), reason

    def test_repeated_construction_is_identical(self) -> None:
        assert a_comparison() == a_comparison()
        assert a_comparison().as_dict() == a_comparison().as_dict()
        assert summarise(a_comparison()) == summarise(a_comparison())

    def test_the_summary_refuses_the_subtraction(self) -> None:
        """Two rupee figures side by side invite a wrong subtraction."""
        text = summarise(a_comparison())
        assert "NOT SPENT" in text
        assert "EARNED" in text
        assert "does not yield a profit figure" in text
        assert "gross margin" in text

    def test_the_summary_labels_the_amount_proxy(self) -> None:
        text = summarise(a_comparison())
        assert "amount_at_risk" in text
        assert "gross proxy" in text

    def test_the_summary_says_avoiding_is_not_free(self) -> None:
        assert "may not happen" in summarise(a_comparison())

    def test_the_summary_is_labelled_synthetic(self) -> None:
        assert "synthetic/demo" in summarise(a_comparison())


# -- against the real database --------------------------------------------


@pytest.mark.db
class TestAgainstTheDatabase:
    def _materialised(self, session: Session):  # noqa: ANN202
        run = materialise(session, seed=SMALL_SEED, case_count=SMALL_CASE_COUNT)
        report = build_report(
            session, run.experiment_id, resamples=FAST_RESAMPLES, include_uplift=True
        )
        return run, report

    def test_the_comparison_has_the_expected_shape(self, db_session: Session) -> None:
        run, report = self._materialised(db_session)
        comparison = run_gate_comparison(
            db_session,
            run.experiment_id,
            report=report,
            as_of=AS_OF,
            resamples=FAST_RESAMPLES,
        )

        assert comparison.n_treatment == report.recovery.n_treatment
        assert comparison.gate_off_actions == comparison.n_treatment
        assert 0 <= comparison.gate_on_actions <= comparison.gate_off_actions
        assert comparison.actions_avoided >= 0
        assert comparison.intervention_code == BENCHMARK_ACTION.value
        assert comparison.unit_cost == 200  # the seeded catalogue value
        assert comparison.ate_bps == report.recovery.ate_bps
        assert comparison.incremental_recovered == report.ledger.incremental_recovered

    def test_the_causal_estimate_is_bit_identical_across_both_passes(
        self, db_session: Session
    ) -> None:
        """The proof that matters.

        The report is built **once**, before either pass. The gate is pure and
        every causal input was materialised beforehand, so the estimate cannot
        move — this asserts that it did not, and that no watched table grew.
        """
        run, report = self._materialised(db_session)

        before = capture_snapshot(db_session, report)

        run_gate_comparison(
            db_session,
            run.experiment_id,
            report=report,
            as_of=AS_OF,
            resamples=FAST_RESAMPLES,
        )

        after = capture_snapshot(db_session, report)

        assert after == before
        assert after.ate_bps == before.ate_bps
        assert after.ci_low_bps == before.ci_low_bps
        assert after.ci_high_bps == before.ci_high_bps
        assert after.incremental_recovered == before.incremental_recovered
        assert after.report_digest == before.report_digest
        assert dict(after.row_counts) == dict(before.row_counts)

    def test_no_watched_table_grew(self, db_session: Session) -> None:
        run, report = self._materialised(db_session)
        before = dict(row_counts(db_session))

        run_gate_comparison(
            db_session,
            run.experiment_id,
            report=report,
            as_of=AS_OF,
            resamples=FAST_RESAMPLES,
        )

        after = dict(row_counts(db_session))
        for table in WATCHED_TABLES:
            assert after[table] == before[table], table
        # An abstention would have written here had the seam been used.
        assert after["audit_events"] == before["audit_events"]
        assert after["recovery_cases"] == before["recovery_cases"]

    def test_it_is_deterministic(self, db_session: Session) -> None:
        run, report = self._materialised(db_session)
        kwargs = {
            "report": report,
            "as_of": AS_OF,
            "resamples": FAST_RESAMPLES,
        }
        first = run_gate_comparison(db_session, run.experiment_id, **kwargs)  # type: ignore[arg-type]
        second = run_gate_comparison(db_session, run.experiment_id, **kwargs)  # type: ignore[arg-type]
        assert first == second
        assert first.as_dict() == second.as_dict()

    def test_the_report_digest_is_stable(self, db_session: Session) -> None:
        _, report = self._materialised(db_session)
        assert report_digest(report) == report_digest(report)
        assert len(report_digest(report)) == 64

    def test_the_default_bootstrap_constants_are_untouched(self) -> None:
        """The comparison uses the pre-registered settings; it defines none."""
        assert BOOTSTRAP_RESAMPLES == 10_000
        assert BOOTSTRAP_SEED == 20_260_830

    def test_a_mismatched_population_is_refused(self, db_session: Session) -> None:
        run, report = self._materialised(db_session)
        with pytest.raises(Exception):  # noqa: B017 - any refusal is acceptable here
            run_gate_comparison(
                db_session,
                uuid.uuid4(),
                report=report,
                as_of=AS_OF,
                resamples=FAST_RESAMPLES,
            )


# -- scenario sensitivity (Day 2.3) ---------------------------------------


def a_scenario_result(
    *,
    policy: GrayZonePolicy = GrayZonePolicy.CURRENT_BASELINE,
    budget: int = 500,
    n_treatment: int = 1_000,
    act: int = 600,
    explored: int = 0,
) -> ScenarioResult:
    return ScenarioResult(
        scenario=Scenario(gray_zone_policy=policy, exploration_budget_bps=budget),
        comparison=a_comparison(n_treatment=n_treatment, gate_on_actions=act, explored=explored),
        amounts=AmountDistribution(n=10, minimum=0, p25=5, median=10, p75=20, maximum=99),
    )


class TestTheGrid:
    def test_it_holds_exactly_eight_scenarios(self) -> None:
        assert len(SCENARIO_GRID) == 8
        assert len(set(SCENARIO_GRID)) == 8

    def test_the_axes_are_the_approved_values(self) -> None:
        assert BUDGET_BPS_GRID == (0, 250, 500, 1_000)
        assert POLICY_GRID == (GrayZonePolicy.CURRENT_BASELINE, GrayZonePolicy.NULL_ONLY)

    def test_exploration_only_was_not_implemented(self) -> None:
        """Dropped deliberately: it had no honest existing AbstainReason."""
        assert {p.value for p in GrayZonePolicy} == {"current_baseline", "null_only"}

    def test_the_grid_is_the_cartesian_product(self) -> None:
        assert {(s.gray_zone_policy, s.exploration_budget_bps) for s in SCENARIO_GRID} == {
            (policy, budget) for policy in POLICY_GRID for budget in BUDGET_BPS_GRID
        }

    def test_cost_is_not_an_axis(self) -> None:
        """The measured catalogue value only; a declared cost would be invented."""
        import dataclasses

        names = {f.name for f in dataclasses.fields(Scenario)}
        assert names == {"gray_zone_policy", "exploration_budget_bps"}

    def test_scenario_names_are_stable_and_distinct(self) -> None:
        names = [s.name for s in SCENARIO_GRID]
        assert len(set(names)) == 8
        assert "current_baseline@500bps" in names


class TestPercentiles:
    def test_zero_and_hundred_are_the_bounds(self) -> None:
        values = [1, 2, 3, 4, 5]
        assert _percentile(values, 0) == 1
        assert _percentile(values, 100) == 5

    def test_the_median_of_an_odd_sequence(self) -> None:
        assert _percentile([1, 2, 3, 4, 5], 50) == 3

    def test_an_even_median_takes_the_lower_value(self) -> None:
        """Averaging would introduce the one float this module avoids."""
        assert _percentile([1, 2, 3, 4], 50) == 2

    def test_a_single_value_is_every_percentile(self) -> None:
        for percent in PERCENTILES:
            assert _percentile([7], percent) == 7

    def test_an_empty_sequence_is_refused(self) -> None:
        with pytest.raises(GateBenchmarkError, match="undefined"):
            _percentile([], 50)

    def test_a_percent_outside_the_range_is_refused(self) -> None:
        with pytest.raises(GateBenchmarkError, match="within 0"):
            _percentile([1, 2], 101)

    def test_the_distribution_serialises_without_floats(self) -> None:
        payload = AmountDistribution(n=4, minimum=0, p25=1, median=2, p75=3, maximum=4).as_dict()
        assert set(payload) == {"n", "min", "p25", "median", "p75", "max"}
        for key, value in payload.items():
            assert isinstance(value, int), key


class TestScenarioResultShape:
    def test_it_carries_policy_and_budget(self) -> None:
        payload = a_scenario_result().as_dict()
        assert payload["gray_zone_policy"] == "current_baseline"
        assert payload["exploration_budget_bps"] == 500
        assert payload["scenario"] == "current_baseline@500bps"

    def test_it_carries_every_required_output(self) -> None:
        payload = a_scenario_result().as_dict()
        assert set(payload) >= {
            "n_treatment",
            "act_count",
            "abstain_count",
            "actions_avoided",
            "avoided_fraction_bps",
            "unit_cost",
            "gate_on_cost",
            "cost_avoided",
            "abstentions",
            "explored",
            "explored_fraction_bps",
            "expected_incremental_recovery",
        }

    def test_act_plus_abstain_is_the_treatment_count(self) -> None:
        result = a_scenario_result(n_treatment=1_000, act=600)
        assert result.act_count + result.abstain_count == result.comparison.n_treatment

    def test_all_twelve_reasons_are_zero_filled(self) -> None:
        counts = dict(a_scenario_result().comparison.abstentions)
        assert len(counts) == 12
        assert set(counts) == {reason.value for reason in AbstainReason}

    def test_no_float_appears_in_any_output(self) -> None:
        payload = a_scenario_result().as_dict()
        for key, value in payload.items():
            assert not isinstance(value, float), key
        for key, value in payload["expected_incremental_recovery"].items():  # type: ignore[union-attr]
            assert isinstance(value, int), key

    def test_the_honesty_text_travels_with_every_scenario(self) -> None:
        payload = a_scenario_result().as_dict()
        text = " ".join(payload["honesty"])  # type: ignore[arg-type]
        assert "not causal estimates" in text
        assert "one ATE" in text
        assert "Neither is P&L" in text
        assert "gross expected-recovery proxy" in text
        assert "absent from this repository" in text

    def test_the_honesty_block_names_every_missing_input(self) -> None:
        text = " ".join(HONESTY).lower()
        for term in ("gross margin", "take rate", "mdr", "commission", "lifetime value"):
            assert term in text


class TestSensitivityRunInvariants:
    def _run(self, results: tuple[ScenarioResult, ...]) -> SensitivityRun:
        return SensitivityRun(
            results=results,
            snapshot=CausalSnapshot(
                ate_bps=1_564,
                ci_low_bps=1_370,
                ci_high_bps=1_757,
                incremental_recovered=447_880_605,
                report_digest="d" * 64,
                row_counts=(("case_assignments", 10_000),),
            ),
            amounts=AmountDistribution(n=1, minimum=0, p25=0, median=0, p75=0, maximum=0),
        )

    def test_scenarios_disagreeing_on_the_population_are_refused(self) -> None:
        with pytest.raises(GateBenchmarkError, match="disagree on the measured population"):
            self._run(
                (
                    a_scenario_result(n_treatment=1_000, act=600),
                    a_scenario_result(n_treatment=999, act=600, budget=250),
                )
            )

    def test_result_for_finds_a_scenario(self) -> None:
        run = self._run((a_scenario_result(),))
        found = run.result_for(Scenario(GrayZonePolicy.CURRENT_BASELINE, 500))
        assert found.scenario.exploration_budget_bps == 500

    def test_an_absent_scenario_is_refused(self) -> None:
        run = self._run((a_scenario_result(),))
        with pytest.raises(GateBenchmarkError, match="no result"):
            run.result_for(Scenario(GrayZonePolicy.NULL_ONLY, 0))

    def test_the_summary_names_every_scenario_and_the_honesty_block(self) -> None:
        run = self._run(tuple(a_scenario_result(budget=b) for b in BUDGET_BPS_GRID))
        text = summarise_sensitivity(run)
        for budget in BUDGET_BPS_GRID:
            assert f"@{budget}bps" in text
        for reason in AbstainReason:
            assert reason.value in text
        for line in HONESTY:
            assert line.split(".")[0] in text
        assert "1,564" in text
        assert "[1,370, 1,757]" in text


# -- the gate itself, under each policy -----------------------------------


class TestGrayZonePolicyInTheGate:
    """The two policies differ on exactly one branch, and nowhere else."""

    def _decide(self, policy: GrayZonePolicy, budget: int, ev: UpliftEvidence):  # noqa: ANN202
        from app.engine.policy_engine import InterventionTerms, decide

        return decide(
            uuid.UUID("11111111-1111-4111-8111-111111111111"),
            uuid.UUID("22222222-2222-4222-8222-222222222222"),
            arm=Arm.TREATMENT,
            uplift=ev,
            intervention=InterventionTerms(
                code="create_payment_link",
                unit_cost=200,
                cooldown_hours=24,
                max_per_customer_per_month=3,
                requires_afa=False,
                is_active=True,
            ),
            expected_recovery=100_000,
            max_cost=100_000,
            as_of=AS_OF,
            budget_bps=budget,
            gray_zone_policy=policy,
        )

    def _null_effect(self) -> UpliftEvidence:
        return UpliftEvidence(
            uplift_bps=50,
            uplift_ci_low_bps=-200,
            uplift_ci_high_bps=300,
            quadrant=Quadrant.GRAY_ZONE,
            qualified=True,
        )

    def _significant(self) -> UpliftEvidence:
        return UpliftEvidence(
            uplift_bps=1_500,
            uplift_ci_low_bps=1_000,
            uplift_ci_high_bps=2_000,
            quadrant=Quadrant.GRAY_ZONE,
            qualified=True,
        )

    def test_the_default_is_the_measured_baseline(self) -> None:
        import inspect

        from app.engine.policy_engine import decide

        default = inspect.signature(decide).parameters["gray_zone_policy"].default
        assert default is GrayZonePolicy.CURRENT_BASELINE

    def test_null_only_abstains_a_null_effect_even_inside_the_budget(self) -> None:
        decision = self._decide(GrayZonePolicy.NULL_ONLY, BPS_SCALE, self._null_effect())
        assert decision.decision is CaseDecision.ABSTAIN
        assert decision.reason is AbstainReason.UPLIFT_NOT_SIGNIFICANT
        assert not decision.explored

    def test_the_baseline_explores_the_same_unit(self) -> None:
        decision = self._decide(GrayZonePolicy.CURRENT_BASELINE, BPS_SCALE, self._null_effect())
        assert decision.decision is CaseDecision.ACT
        assert decision.explored

    def test_neither_policy_acts_ordinarily_on_a_significant_gray_zone_unit(self) -> None:
        """Inverted by DR-4, and the inversion is the whole point.

        This test previously asserted that both policies **act** on these units.
        That was an accurate description of the gate and an inaccurate one of
        what the gate should do: on the accepted N=10,000 population it covered
        1,757 of 4,124 actions, every one of them a significant lift on
        customers who largely recover unaided.

        What `GrayZonePolicy` still guarantees is unchanged and is asserted
        here: the two policies remain identical on this half. Neither may act
        outside the exploration budget, and the reason must name the real
        objection — the value of acting, not the strength of the evidence.
        """
        for policy in POLICY_GRID:
            decision = self._decide(policy, 0, self._significant())
            assert decision.decision is CaseDecision.ABSTAIN, policy
            assert decision.reason is AbstainReason.SELF_RECOVERY_LIKELY, policy
            assert not decision.explored

    def test_both_policies_still_explore_a_significant_gray_zone_unit(self) -> None:
        """Abstaining is not abandoning: the budget still learns about them."""
        for policy in POLICY_GRID:
            decision = self._decide(policy, BPS_SCALE, self._significant())
            assert decision.decision is CaseDecision.ACT, policy
            assert decision.explored, policy

    def test_at_zero_budget_the_policies_are_identical(self) -> None:
        for evidence_case in (self._null_effect(), self._significant()):
            baseline = self._decide(GrayZonePolicy.CURRENT_BASELINE, 0, evidence_case)
            null_only = self._decide(GrayZonePolicy.NULL_ONLY, 0, evidence_case)
            assert baseline.decision == null_only.decision
            assert baseline.reason == null_only.reason

    def test_null_only_uses_only_existing_abstain_reasons(self) -> None:
        decision = self._decide(GrayZonePolicy.NULL_ONLY, BPS_SCALE, self._null_effect())
        assert decision.reason is not None
        assert decision.reason.value in AbstainReason.values()

    def test_no_thirteenth_reason_was_invented(self) -> None:
        """A new value would need a migration to rewrite the CHECK constraint.

        Twelve since `self_recovery_likely` (DR-4), which arrived with exactly
        one migration — 9c41e07b2d58 — rewriting
        `ck_recovery_cases_abstain_reason_valid` and nothing else.
        """
        assert len(AbstainReason.values()) == 12


# -- against the real database --------------------------------------------


@pytest.mark.db
class TestSensitivityAgainstTheDatabase:
    def _materialised(self, session: Session):  # noqa: ANN202
        run = materialise(session, seed=SMALL_SEED + 1, case_count=SMALL_CASE_COUNT)
        report = build_report(
            session, run.experiment_id, resamples=FAST_RESAMPLES, include_uplift=True
        )
        return run, report

    def _sensitivity(self, session: Session):  # noqa: ANN202
        run, report = self._materialised(session)
        return run_sensitivity(
            session, run.experiment_id, report=report, resamples=FAST_RESAMPLES
        ), report

    def test_eight_scenarios_are_complete(self, db_session: Session) -> None:
        sensitivity, report = self._sensitivity(db_session)
        assert len(sensitivity.results) == 8
        for result in sensitivity.results:
            c = result.comparison
            assert result.act_count + result.abstain_count == c.n_treatment
            assert c.n_treatment == report.recovery.n_treatment
            assert len(c.abstentions) == 12
            assert c.gate_on_actions <= c.gate_off_actions

    def test_act_is_non_decreasing_in_the_budget(self, db_session: Session) -> None:
        sensitivity, _ = self._sensitivity(db_session)
        for policy in POLICY_GRID:
            counts = [
                sensitivity.result_for(Scenario(policy, budget)).act_count
                for budget in BUDGET_BPS_GRID
            ]
            assert counts == sorted(counts), (policy, counts)

    def test_null_only_never_acts_more_than_the_baseline(self, db_session: Session) -> None:
        sensitivity, _ = self._sensitivity(db_session)
        for budget in BUDGET_BPS_GRID:
            baseline = sensitivity.result_for(
                Scenario(GrayZonePolicy.CURRENT_BASELINE, budget)
            ).act_count
            null_only = sensitivity.result_for(Scenario(GrayZonePolicy.NULL_ONLY, budget)).act_count
            assert null_only <= baseline, budget

    def test_at_zero_budget_the_two_policies_agree(self, db_session: Session) -> None:
        sensitivity, _ = self._sensitivity(db_session)
        baseline = sensitivity.result_for(Scenario(GrayZonePolicy.CURRENT_BASELINE, 0))
        null_only = sensitivity.result_for(Scenario(GrayZonePolicy.NULL_ONLY, 0))
        assert baseline.comparison.as_dict() == null_only.comparison.as_dict()
        assert baseline.comparison.explored == 0

    def test_the_baseline_scenario_matches_run_gate_comparison(self, db_session: Session) -> None:
        """`CURRENT_BASELINE @ 500` must be the measured gate, not a variant."""
        run, report = self._materialised(db_session)
        sensitivity = run_sensitivity(
            db_session, run.experiment_id, report=report, resamples=FAST_RESAMPLES
        )
        measured = run_gate_comparison(
            db_session, run.experiment_id, report=report, resamples=FAST_RESAMPLES
        )
        scenario = sensitivity.result_for(
            Scenario(GrayZonePolicy.CURRENT_BASELINE, EXPLORATION_BUDGET_BPS)
        )
        assert scenario.comparison.gate_on_actions == measured.gate_on_actions
        assert dict(scenario.comparison.abstentions) == dict(measured.abstentions)
        assert scenario.comparison.explored == measured.explored

    def test_the_causal_snapshot_is_identical_across_all_scenarios(
        self, db_session: Session
    ) -> None:
        run, report = self._materialised(db_session)
        before = capture_snapshot(db_session, report)
        sensitivity = run_sensitivity(
            db_session, run.experiment_id, report=report, resamples=FAST_RESAMPLES
        )
        after = capture_snapshot(db_session, report)

        assert before == after
        assert sensitivity.snapshot == before
        for result in sensitivity.results:
            assert result.comparison.ate_bps == before.ate_bps
            assert result.comparison.incremental_recovered == before.incremental_recovered

    def test_assign_quadrants_is_called_exactly_once(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Eight scenarios, one cross-fit. Labels do not depend on the policy."""
        import tests.benchmark.gate_benchmark as module

        run, report = self._materialised(db_session)
        calls: list[int] = []
        original = module.assign_quadrants

        def spy(*args: object, **kwargs: object):  # noqa: ANN202
            calls.append(1)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(module, "assign_quadrants", spy)
        run_sensitivity(db_session, run.experiment_id, report=report, resamples=FAST_RESAMPLES)
        assert len(calls) == 1

    def test_it_writes_nothing(self, db_session: Session) -> None:
        run, report = self._materialised(db_session)
        before = dict(row_counts(db_session))
        experiment_results_before = db_session.execute(
            select(func.count()).select_from(ExperimentResult)
        ).scalar_one()

        run_sensitivity(db_session, run.experiment_id, report=report, resamples=FAST_RESAMPLES)

        assert dict(row_counts(db_session)) == before
        assert (
            db_session.execute(select(func.count()).select_from(ExperimentResult)).scalar_one()
            == experiment_results_before
        )

    def test_repeated_execution_is_identical(self, db_session: Session) -> None:
        run, report = self._materialised(db_session)
        first = run_sensitivity(
            db_session, run.experiment_id, report=report, resamples=FAST_RESAMPLES
        )
        second = run_sensitivity(
            db_session, run.experiment_id, report=report, resamples=FAST_RESAMPLES
        )
        assert first.as_dict() == second.as_dict()

    def test_the_distribution_is_policy_independent(self, db_session: Session) -> None:
        sensitivity, _ = self._sensitivity(db_session)
        distributions = {result.amounts for result in sensitivity.results}
        assert len(distributions) == 1
        only = distributions.pop()
        assert only.minimum <= only.p25 <= only.median <= only.p75 <= only.maximum
        assert only.n > 0

    def test_the_summary_renders(self, db_session: Session) -> None:
        sensitivity, _ = self._sensitivity(db_session)
        text = summarise_sensitivity(sensitivity)
        assert "SCENARIO SENSITIVITY" in text
        assert "current_baseline@500bps" in text
        assert "null_only@0bps" in text
