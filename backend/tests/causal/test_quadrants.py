"""Quadrant assignment: the five labels, their order, and their boundaries.

The rules are tested against hand-built scores where the expected label can be
read off the numbers, and the boundaries are tested one basis point either side.
Precedence gets its own class, because the order of the rules is the contract —
a unit that satisfies three of them must get the one that fires first.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid

import pytest

from app.causal.cells import Features, Unit, fold_of
from app.causal.estimators import Interval
from app.causal.quadrants import (
    RULE_HARMFUL,
    RULE_HIGH_BASELINE,
    RULE_LOW_BASELINE,
    RULE_NEGATIVE_UPLIFT,
    RULE_NOT_QUALIFYING,
    RULE_SIGNIFICANT_UPLIFT,
    RULE_UNDECIDED,
    FoldThresholds,
    QuadrantError,
    assign_quadrants,
    classify,
    derive_thresholds,
)
from app.causal.uplift import MODEL_VERSION, UpliftScore, fit_fold
from app.models.enums import Arm, Quadrant

ALPHA = 500
MDE = 1_000
FAST = 100
EXPERIMENT_ID = uuid.UUID("eeeeeeee-0000-4000-8000-000000000006")

#: A fold's boundaries, fixed so the rules can be tested against known numbers.
THRESHOLDS = FoldThresholds(
    fold=0,
    self_recovery_ceiling_bps=4_000,
    low_tertile_bps=2_000,
    high_tertile_bps=6_000,
    harm_threshold_bps=300,
    training_size=8_000,
    qualifying_units=8_000,
)


def a_score(
    *,
    uplift_bps: int = 0,
    ci_low: int = -100,
    ci_high: int = 100,
    p_control_bps: int = 3_000,
    harm_uplift_bps: int = 0,
    qualified: bool = True,
) -> UpliftScore:
    return UpliftScore(
        risk_id=uuid.uuid4(),
        model_version=MODEL_VERSION,
        fold=0,
        level=0 if qualified else None,
        level_name="failure_code|payment_method" if qualified else None,
        cell_key="cell" if qualified else "(global)",
        p_treat_bps=p_control_bps + uplift_bps,
        p_control_bps=p_control_bps,
        uplift_bps=uplift_bps,
        interval=Interval(low=ci_low, high=ci_high, alpha_bps=ALPHA, resamples=FAST, seed=1),
        harm_uplift_bps=harm_uplift_bps,
        qualified=qualified,
        reason="qualified" if qualified else "global_fallback",
        n_treated=800,
        n_holdout=800,
    )


class TestEveryQuadrant:
    def test_a_non_qualifying_cell_is_gray_zone(self) -> None:
        quadrant, rule = classify(a_score(qualified=False), THRESHOLDS)
        assert quadrant is Quadrant.GRAY_ZONE
        assert rule == RULE_NOT_QUALIFYING

    def test_a_wholly_negative_interval_is_a_sleeping_dog(self) -> None:
        quadrant, rule = classify(a_score(uplift_bps=-500, ci_low=-900, ci_high=-100), THRESHOLDS)
        assert quadrant is Quadrant.SLEEPING_DOG
        assert rule == RULE_NEGATIVE_UPLIFT

    def test_harm_above_the_threshold_is_a_sleeping_dog(self) -> None:
        """Acting lifts recovery but destroys mandates. That is not a success."""
        quadrant, rule = classify(
            a_score(uplift_bps=800, ci_low=400, ci_high=1_200, harm_uplift_bps=900),
            THRESHOLDS,
        )
        assert quadrant is Quadrant.SLEEPING_DOG
        assert rule == RULE_HARMFUL

    def test_a_significant_lift_below_the_ceiling_is_persuadable(self) -> None:
        quadrant, rule = classify(
            a_score(uplift_bps=800, ci_low=400, ci_high=1_200, p_control_bps=3_000),
            THRESHOLDS,
        )
        assert quadrant is Quadrant.PERSUADABLE
        assert rule == RULE_SIGNIFICANT_UPLIFT

    def test_a_null_effect_on_a_high_baseline_is_a_sure_thing(self) -> None:
        """They were going to pay anyway."""
        quadrant, rule = classify(
            a_score(uplift_bps=50, ci_low=-200, ci_high=300, p_control_bps=7_000),
            THRESHOLDS,
        )
        assert quadrant is Quadrant.SURE_THING
        assert rule == RULE_HIGH_BASELINE

    def test_a_null_effect_on_a_low_baseline_is_a_lost_cause(self) -> None:
        """They were never going to pay."""
        quadrant, rule = classify(
            a_score(uplift_bps=10, ci_low=-200, ci_high=300, p_control_bps=1_000),
            THRESHOLDS,
        )
        assert quadrant is Quadrant.LOST_CAUSE
        assert rule == RULE_LOW_BASELINE

    def test_everything_else_is_gray_zone(self) -> None:
        """A null effect on a middling baseline is genuinely undecided."""
        quadrant, rule = classify(
            a_score(uplift_bps=10, ci_low=-200, ci_high=300, p_control_bps=4_000),
            THRESHOLDS,
        )
        assert quadrant is Quadrant.GRAY_ZONE
        assert rule == RULE_UNDECIDED

    def test_all_five_labels_are_reachable(self) -> None:
        reached = {
            classify(score, THRESHOLDS)[0]
            for score in (
                a_score(qualified=False),
                a_score(ci_low=-900, ci_high=-100),
                a_score(ci_low=400, ci_high=1_200, p_control_bps=3_000),
                a_score(ci_low=-200, ci_high=300, p_control_bps=7_000),
                a_score(ci_low=-200, ci_high=300, p_control_bps=1_000),
            )
        }
        assert reached == set(Quadrant)


class TestExactBoundaries:
    def test_the_negative_interval_boundary(self) -> None:
        """`ci_high < 0` is strict: an interval touching zero is not negative."""
        assert classify(a_score(ci_low=-900, ci_high=-1), THRESHOLDS)[0] is Quadrant.SLEEPING_DOG
        assert classify(a_score(ci_low=-900, ci_high=0), THRESHOLDS)[0] is not Quadrant.SLEEPING_DOG

    def test_the_harm_boundary(self) -> None:
        """`harm > threshold` is strict: exactly at the threshold is not harm."""
        at = a_score(ci_low=-200, ci_high=300, p_control_bps=7_000, harm_uplift_bps=300)
        over = a_score(ci_low=-200, ci_high=300, p_control_bps=7_000, harm_uplift_bps=301)
        assert classify(at, THRESHOLDS)[0] is Quadrant.SURE_THING
        assert classify(over, THRESHOLDS)[0] is Quadrant.SLEEPING_DOG

    def test_the_persuadable_significance_boundary(self) -> None:
        """`ci_low > 0` is strict: an interval touching zero is not significant."""
        strict = a_score(ci_low=1, ci_high=900, p_control_bps=3_000)
        touching = a_score(ci_low=0, ci_high=900, p_control_bps=3_000)
        assert classify(strict, THRESHOLDS)[0] is Quadrant.PERSUADABLE
        assert classify(touching, THRESHOLDS)[0] is not Quadrant.PERSUADABLE

    def test_the_self_recovery_ceiling_boundary(self) -> None:
        """`p_control < ceiling` is strict: exactly at the ceiling is not below."""
        below = a_score(ci_low=400, ci_high=1_200, p_control_bps=3_999)
        at = a_score(ci_low=400, ci_high=1_200, p_control_bps=4_000)
        assert classify(below, THRESHOLDS)[0] is Quadrant.PERSUADABLE
        assert classify(at, THRESHOLDS)[0] is not Quadrant.PERSUADABLE

    def test_the_high_tertile_boundary(self) -> None:
        """`p_control >= high` is inclusive."""
        at = a_score(ci_low=-200, ci_high=300, p_control_bps=6_000)
        below = a_score(ci_low=-200, ci_high=300, p_control_bps=5_999)
        assert classify(at, THRESHOLDS)[0] is Quadrant.SURE_THING
        assert classify(below, THRESHOLDS)[0] is Quadrant.GRAY_ZONE

    def test_the_low_tertile_boundary(self) -> None:
        """`p_control <= low` is inclusive."""
        at = a_score(ci_low=-200, ci_high=300, p_control_bps=2_000)
        above = a_score(ci_low=-200, ci_high=300, p_control_bps=2_001)
        assert classify(at, THRESHOLDS)[0] is Quadrant.LOST_CAUSE
        assert classify(above, THRESHOLDS)[0] is Quadrant.GRAY_ZONE

    def test_an_interval_containing_zero_at_its_edge_still_contains_it(self) -> None:
        assert (
            classify(a_score(ci_low=0, ci_high=500, p_control_bps=7_000), THRESHOLDS)[0]
            is Quadrant.SURE_THING
        )
        assert (
            classify(a_score(ci_low=-500, ci_high=0, p_control_bps=7_000), THRESHOLDS)[0]
            is Quadrant.SURE_THING
        )


class TestPrecedence:
    def test_not_qualifying_beats_everything(self) -> None:
        """A thin cell cannot be labelled, however striking its numbers."""
        striking = a_score(
            uplift_bps=-5_000,
            ci_low=-6_000,
            ci_high=-4_000,
            harm_uplift_bps=9_000,
            qualified=False,
        )
        assert classify(striking, THRESHOLDS) == (Quadrant.GRAY_ZONE, RULE_NOT_QUALIFYING)

    def test_harm_beats_persuasion(self) -> None:
        """A cell that lifts recovery *and* destroys mandates is a sleeping dog,
        not a persuadable. This is the whole point of the ordering."""
        both = a_score(
            uplift_bps=900, ci_low=500, ci_high=1_300, p_control_bps=1_000, harm_uplift_bps=5_000
        )
        assert classify(both, THRESHOLDS)[0] is Quadrant.SLEEPING_DOG

    def test_a_negative_interval_beats_the_harm_rule(self) -> None:
        """Both point at sleeping dog; the reported reason is the first."""
        both = a_score(ci_low=-900, ci_high=-100, harm_uplift_bps=5_000)
        assert classify(both, THRESHOLDS) == (Quadrant.SLEEPING_DOG, RULE_NEGATIVE_UPLIFT)

    def test_persuadable_beats_the_null_rules(self) -> None:
        """A significant lift is not a null result, so the tertile rules never
        get a chance to call it a sure thing."""
        lifting = a_score(ci_low=400, ci_high=1_200, p_control_bps=1_000)
        assert classify(lifting, THRESHOLDS)[0] is Quadrant.PERSUADABLE

    def test_sure_thing_beats_lost_cause_when_the_tertiles_collapse(self) -> None:
        """With every cell at the same control rate the tertiles coincide and a
        unit satisfies both null rules. Order decides, deterministically."""
        collapsed = FoldThresholds(
            fold=0,
            self_recovery_ceiling_bps=4_000,
            low_tertile_bps=3_000,
            high_tertile_bps=3_000,
            harm_threshold_bps=300,
            training_size=100,
            qualifying_units=100,
        )
        both = a_score(ci_low=-200, ci_high=300, p_control_bps=3_000)
        assert classify(both, collapsed) == (Quadrant.SURE_THING, RULE_HIGH_BASELINE)


class TestHarmEdges:
    def test_zero_harm_never_triggers_the_rule(self) -> None:
        assert (
            classify(a_score(ci_low=400, ci_high=1_200, harm_uplift_bps=0), THRESHOLDS)[0]
            is Quadrant.PERSUADABLE
        )

    def test_negative_harm_never_triggers_the_rule(self) -> None:
        """Contact that *reduced* cancellations is not harm."""
        assert (
            classify(a_score(ci_low=400, ci_high=1_200, harm_uplift_bps=-800), THRESHOLDS)[0]
            is Quadrant.PERSUADABLE
        )

    def test_a_negative_harm_threshold_still_compares_correctly(self) -> None:
        """A training fold whose harm interval sat below zero yields a negative
        threshold; the strict comparison must still hold."""
        negative = FoldThresholds(
            fold=0,
            self_recovery_ceiling_bps=4_000,
            low_tertile_bps=2_000,
            high_tertile_bps=6_000,
            harm_threshold_bps=-200,
            training_size=100,
            qualifying_units=100,
        )
        assert (
            classify(a_score(ci_low=400, ci_high=1_200, harm_uplift_bps=-200), negative)[0]
            is Quadrant.PERSUADABLE
        )
        assert (
            classify(a_score(ci_low=400, ci_high=1_200, harm_uplift_bps=-199), negative)[0]
            is Quadrant.SLEEPING_DOG
        )


# -- end to end over a constructed population -----------------------------


def features(failure_code: str, payment_method: str = "upi") -> Features:
    return Features(
        failure_code=failure_code,
        payment_method=payment_method,
        amount_band="2000-5000",
        hour_bucket="morning",
        tenure_bucket="established",
        salary_window=False,
    )


def build(spec: dict[str, tuple[int, int, int, int]], *, harm: dict[str, int] | None = None):  # noqa: ANN201
    """Units per failure code: (n_t, recovered_t, n_h, recovered_h)."""
    import random

    rng = random.Random(20260901)
    harm = harm or {}
    units: list[Unit] = []

    for code, (n_t, r_t, n_h, r_h) in spec.items():
        budget = harm.get(code, 0)
        for index in range(n_t):
            units.append(
                Unit(
                    risk_id=uuid.UUID(int=rng.getrandbits(128), version=4),
                    arm=Arm.TREATMENT.value,
                    recovered=index < r_t,
                    harmed=index < budget,
                    features=features(code),
                )
            )
        for index in range(n_h):
            units.append(
                Unit(
                    risk_id=uuid.UUID(int=rng.getrandbits(128), version=4),
                    arm=Arm.HOLDOUT.value,
                    recovered=index < r_h,
                    harmed=False,
                    features=features(code),
                )
            )
    return units


#: Three cells with clearly different characters, all comfortably qualifying.
POPULATION = {
    "gateway_timeout": (2_500, 1_875, 2_500, 1_850),  # high baseline, no lift
    "insufficient_funds": (2_500, 1_250, 2_500, 750),  # low baseline, real lift
    "card_declined": (2_500, 100, 2_500, 95),  # low baseline, no lift
}


class TestFoldLocalThresholds:
    def test_each_fold_gets_its_own(self) -> None:
        units = build(POPULATION)
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        assert len(run.thresholds) == 5
        assert [t.fold for t in run.thresholds] == [0, 1, 2, 3, 4]

    def test_a_threshold_uses_only_its_training_folds(self) -> None:
        """The isolation that makes cross-fitting mean anything: the numbers a
        fold is judged against are computed without it."""
        units = build(POPULATION)
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        training = [u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) != 0]
        held_out = [u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0]

        thresholds = derive_thresholds(model, training)
        assert thresholds.training_size == len(training)
        assert thresholds.qualifying_units == len(training)
        assert len(held_out) > 0
        assert thresholds.training_size + len(held_out) == len(units)

    def test_changing_a_held_out_outcome_does_not_move_its_own_threshold(self) -> None:
        """The leak this design exists to prevent, tested directly."""
        from dataclasses import replace

        units = build(POPULATION)
        held_out_ids = {u.risk_id for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0}
        flipped = [
            replace(u, recovered=not u.recovered) if u.risk_id in held_out_ids else u for u in units
        ]

        def thresholds_for(population: list[Unit]) -> dict[str, object]:
            model = fit_fold(
                population, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST
            )
            training = [u for u in population if fold_of(u.risk_id, EXPERIMENT_ID) != 0]
            return derive_thresholds(model, training).as_dict()

        assert thresholds_for(units) == thresholds_for(flipped)

    def test_the_ceiling_is_the_training_holdout_rate(self) -> None:
        units = build(POPULATION)
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        training = [u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) != 0]
        thresholds = derive_thresholds(model, training)
        assert thresholds.self_recovery_ceiling_bps == model.global_counts.p_control_bps

    def test_the_tertiles_are_ordered(self) -> None:
        units = build(POPULATION)
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        for thresholds in run.thresholds:
            assert thresholds.low_tertile_bps <= thresholds.high_tertile_bps

    def test_the_tertiles_can_collapse_on_a_tied_list(self) -> None:
        """Arithmetic, not a fault, and pinned on the helper because whether it
        happens in a given fold depends on that fold's exact size.

        These are order statistics over a heavily tied list — every unit carries
        its cell's rate — so with few distinct rates both indices can land in
        the same block. A reader of the report needs to know when the two
        null-effect bands have merged, which is why both are reported.
        """
        from app.causal.quadrants import _tertiles

        assert _tertiles([1] * 3 + [2] * 6 + [3] * 3) == (2, 2)
        assert _tertiles([1] * 4 + [2] * 4 + [3] * 4) == (2, 3)

    def test_a_collapse_is_observed_in_practice(self) -> None:
        """Not hypothetical: at three equal cells some folds collapse and some
        do not, purely on how many units each fold happened to hold out."""
        units = build(POPULATION)
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        collapsed = [t for t in run.thresholds if t.low_tertile_bps == t.high_tertile_bps]
        assert collapsed, "expected at least one fold to collapse at three equal cells"

    def test_the_three_planted_characters_all_appear(self) -> None:
        """Precedence carries it even where the bands merged: the middle cell
        has a significant lift, so Persuadable fires before either null rule."""
        units = build(POPULATION)
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        labels = {a.quadrant for a in run.assignments}
        assert Quadrant.PERSUADABLE in labels
        assert Quadrant.SURE_THING in labels
        assert Quadrant.LOST_CAUSE in labels

    def test_more_cells_separate_the_tertiles(self) -> None:
        """Six distinct control rates give the boundaries room to land in
        different blocks, which is the ordinary case at benchmark size."""
        units = build(
            {
                "gateway_timeout": (1_500, 1_125, 1_500, 1_110),
                "mandate_inactive": (1_500, 960, 1_500, 945),
                "insufficient_funds": (1_500, 750, 1_500, 450),
                "bank_unavailable": (1_500, 600, 1_500, 585),
                "card_declined": (1_500, 300, 1_500, 285),
                "expired_card": (1_500, 90, 1_500, 85),
            }
        )
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        for thresholds in run.thresholds:
            assert thresholds.low_tertile_bps < thresholds.high_tertile_bps


class TestTheEndToEndRun:
    def test_every_unit_is_labelled_once(self) -> None:
        units = build(POPULATION)
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        assert len(run.assignments) == len(units)
        assert len({a.risk_id for a in run.assignments}) == len(units)

    def test_the_planted_characters_come_out(self) -> None:
        """A high-baseline null cell should read Sure Thing, a genuinely
        lifting low-baseline cell Persuadable."""
        units = build(POPULATION)
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        by_code: dict[str, list[Quadrant]] = {}
        for assignment, unit in zip(
            sorted(run.assignments, key=lambda a: a.risk_id),
            sorted(units, key=lambda u: u.risk_id),
            strict=True,
        ):
            by_code.setdefault(unit.features.failure_code, []).append(assignment.quadrant)

        def modal(code: str) -> Quadrant:
            values = by_code[code]
            return max(set(values), key=values.count)

        assert modal("gateway_timeout") is Quadrant.SURE_THING
        assert modal("insufficient_funds") is Quadrant.PERSUADABLE
        assert modal("card_declined") is Quadrant.LOST_CAUSE

    def test_a_planted_harmful_cell_reads_sleeping_dog(self) -> None:
        units = build(
            {
                "gateway_timeout": (2_500, 1_875, 2_500, 1_850),
                "mandate_inactive": (2_500, 1_600, 2_500, 1_575),
                "card_declined": (2_500, 100, 2_500, 95),
            },
            harm={"mandate_inactive": 700},
        )
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        labels = [
            a.quadrant
            for a, u in zip(
                sorted(run.assignments, key=lambda x: x.risk_id),
                sorted(units, key=lambda x: x.risk_id),
                strict=True,
            )
            if u.features.failure_code == "mandate_inactive"
        ]
        assert max(set(labels), key=labels.count) is Quadrant.SLEEPING_DOG

    def test_a_thin_population_is_all_gray_zone(self) -> None:
        units = build({"a": (40, 20, 40, 15), "b": (40, 20, 40, 15)})
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        assert run.counts[Quadrant.GRAY_ZONE.value] == len(units)
        assert run.rule_counts[RULE_NOT_QUALIFYING] == len(units)

    def test_the_counts_cover_every_label(self) -> None:
        units = build(POPULATION)
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        assert set(run.counts) == {q.value for q in Quadrant}
        assert sum(run.counts.values()) == len(units)

    def test_only_persuadable_is_actionable(self) -> None:
        units = build(POPULATION)
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        for assignment in run.assignments:
            assert assignment.is_actionable == (assignment.quadrant is Quadrant.PERSUADABLE)

    def test_an_empty_population_yields_nothing(self) -> None:
        run = assign_quadrants([], EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE)
        assert run.assignments == ()
        assert run.thresholds == ()

    def test_too_few_folds_are_refused(self) -> None:
        with pytest.raises(QuadrantError, match="at least two folds"):
            assign_quadrants(
                build(POPULATION), EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, folds=1
            )


class TestDeterminism:
    def test_it_repeats_exactly(self) -> None:
        units = build(POPULATION)
        first = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        second = assign_quadrants(
            units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST
        )
        assert first.as_dict() == second.as_dict()
        assert [a.as_dict() for a in first.assignments] == [a.as_dict() for a in second.assignments]

    def test_input_order_does_not_change_the_labels(self) -> None:
        import random

        units = build(POPULATION)
        shuffled = list(units)
        random.Random(11).shuffle(shuffled)

        plain = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        mixed = assign_quadrants(
            shuffled, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST
        )
        assert [a.as_dict() for a in plain.assignments] == [a.as_dict() for a in mixed.assignments]

    def test_results_are_ordered_by_risk_id(self) -> None:
        units = build(POPULATION)
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        assert [a.risk_id for a in run.assignments] == sorted(a.risk_id for a in run.assignments)

    def test_the_payload_carries_no_float(self) -> None:
        units = build(POPULATION)
        run = assign_quadrants(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)

        def walk(value: object) -> None:
            assert not isinstance(value, float), value
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(run.as_dict())
        walk(run.assignments[0].as_dict())


class TestPurity:
    @staticmethod
    def _tree() -> ast.Module:
        from app.causal import quadrants as module

        return ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))

    @classmethod
    def _identifiers(cls) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(cls._tree()):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.ClassDef):
                found.add(node.name)
        return found

    @classmethod
    def _imports(cls) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(cls._tree()):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
        return found

    def test_it_never_names_ground_truth(self) -> None:
        for name in self._identifiers():
            assert not name.startswith("truth_"), name

    def test_it_never_names_a_potential_outcome(self) -> None:
        for banned in ("y0", "y1", "harm0", "harm1", "segment_id", "truth_segment"):
            assert banned not in self._identifiers(), banned

    def test_it_imports_no_generator_module(self) -> None:
        for module in self._imports():
            assert not module.startswith("simulator"), module

    def test_it_touches_no_database(self) -> None:
        identifiers = self._identifiers()
        for banned in ("Session", "select", "execute", "commit", "add", "flush", "session"):
            assert banned not in identifiers, banned

    def test_it_persists_nothing(self) -> None:
        """The only model import is the enum of labels."""
        model_imports = {m for m in self._imports() if m.startswith("app.models")}
        assert model_imports == {"app.models.enums"}

    def test_it_builds_no_confusion_matrix(self) -> None:
        """Comparing a label to the answer key belongs to the reporter, which
        is the only part of the application allowed to read it."""
        identifiers = self._identifiers()
        for banned in ("confusion", "confusion_matrix", "expected_quadrant"):
            assert banned not in identifiers, banned

    def test_harm_is_never_persisted(self) -> None:
        """It decides a label and is then discarded. No column, no writer."""
        identifiers = self._identifiers()
        assert "harm_uplift_bps" in identifiers
        assert "UpliftScore" not in {m.rsplit(".", 1)[-1] for m in self._imports()}
        for banned in ("harm_uplift_column", "add_column", "Column"):
            assert banned not in identifiers, banned

    def test_there_is_no_true_division(self) -> None:
        for node in ast.walk(self._tree()):
            assert not isinstance(node, ast.Div), ast.dump(node)

    def test_no_float_constant_appears(self) -> None:
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), node.value

    def test_it_reads_no_clock_and_draws_no_randomness(self) -> None:
        for banned in ("now", "utcnow", "today", "random", "choices"):
            assert banned not in self._identifiers(), banned
