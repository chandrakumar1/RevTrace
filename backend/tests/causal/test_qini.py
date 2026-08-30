"""The Qini curve, checked against rankings whose answers are known by hand.

Populations are built so the shape of the curve is predictable: a perfect
ranking puts every responder first, a reversed one puts them last, and a tied
one has no information at all. Each case has an answer that can be reasoned
about without running the code, which is what makes the assertions worth
anything.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid

import pytest

from app.causal.qini import (
    BPS_SCALE,
    TOP_SHARE_BPS,
    QiniError,
    RankedUnit,
    evaluate,
    qini_coefficient_bps,
    qini_curve,
    rank,
    round_half_up,
    top_capture,
)


def rid(index: int) -> uuid.UUID:
    """A risk id that sorts in the same order as its index."""
    return uuid.UUID(int=index)


def population(
    *,
    responders: int,
    non_responders: int,
    responder_uplift: int,
    other_uplift: int,
    responder_treated_recovers: bool = True,
    responder_holdout_recovers: bool = False,
) -> list[RankedUnit]:
    """Two groups, each split evenly into arms, **returned in ranked order**.

    `responders` recover when treated and not when held out — a real effect.
    `non_responders` never recover in either arm — no effect. The predicted
    uplift attached to each group is what the ranking uses.

    Ordering goes through `rank()` rather than being assumed from construction
    order. Returning the list unranked made the uplift arguments inert, and
    "perfect ranking" then passed only because responders happened to be built
    first — a test that would have accepted any ranking at all.
    """
    units: list[RankedUnit] = []
    index = 0

    for _ in range(responders):
        units.append(RankedUnit(rid(index), responder_uplift, True, responder_treated_recovers))
        index += 1
        units.append(RankedUnit(rid(index), responder_uplift, False, responder_holdout_recovers))
        index += 1

    for _ in range(non_responders):
        units.append(RankedUnit(rid(index), other_uplift, True, False))
        index += 1
        units.append(RankedUnit(rid(index), other_uplift, False, False))
        index += 1

    arms = {unit.risk_id: (unit.is_treatment, unit.recovered) for unit in units}
    return list(rank(arms, [_score(unit) for unit in units]))


class TestRounding:
    def test_it_rounds_half_away_from_zero(self) -> None:
        assert round_half_up(1, 2) == 1
        assert round_half_up(3, 2) == 2
        assert round_half_up(-1, 2) == -1
        assert round_half_up(-3, 2) == -2

    def test_it_rounds_to_nearest_otherwise(self) -> None:
        assert round_half_up(4, 3) == 1
        assert round_half_up(5, 3) == 2
        assert round_half_up(-4, 3) == -1

    def test_it_is_exact_on_whole_numbers(self) -> None:
        assert round_half_up(10, 5) == 2
        assert round_half_up(-10, 5) == -2

    def test_it_is_symmetric_in_sign(self) -> None:
        for numerator in range(-40, 41):
            assert round_half_up(numerator, 7) == -round_half_up(-numerator, 7)

    def test_a_non_positive_denominator_is_refused(self) -> None:
        with pytest.raises(QiniError, match="must be positive"):
            round_half_up(1, 0)
        with pytest.raises(QiniError, match="must be positive"):
            round_half_up(1, -2)


class TestTheCurve:
    def test_it_has_a_point_per_unit(self) -> None:
        units = population(responders=50, non_responders=50, responder_uplift=1, other_uplift=0)
        assert qini_curve(units).n == len(units)

    def test_it_counts_both_arms(self) -> None:
        units = population(responders=50, non_responders=50, responder_uplift=1, other_uplift=0)
        curve = qini_curve(units)
        assert curve.n_treated == 100
        assert curve.n_holdout == 100

    def test_the_total_is_the_incremental_recovery(self) -> None:
        """100 responders treated recover, none of their controls do, and the
        non-responders contribute nothing either way."""
        units = population(responders=100, non_responders=100, responder_uplift=1, other_uplift=0)
        assert qini_curve(units).total == 100

    def test_nothing_chosen_captures_nothing(self) -> None:
        units = population(responders=50, non_responders=50, responder_uplift=1, other_uplift=0)
        assert qini_curve(units).at(0) == 0

    def test_a_prefix_beyond_the_population_is_refused(self) -> None:
        curve = qini_curve(
            population(responders=10, non_responders=10, responder_uplift=1, other_uplift=0)
        )
        with pytest.raises(QiniError, match="k must be within"):
            curve.at(curve.n + 1)
        with pytest.raises(QiniError, match="k must be within"):
            curve.at(-1)

    def test_a_prefix_with_no_controls_takes_its_treated_count(self) -> None:
        """The correction is 0/0 there. Taken as zero rather than refusing to
        plot the first points of every curve."""
        units = [
            RankedUnit(rid(0), 500, True, True),
            RankedUnit(rid(1), 400, True, True),
            RankedUnit(rid(2), 300, False, False),
        ]
        curve = qini_curve(units)
        assert curve.at(1) == 1
        assert curve.at(2) == 2

    def test_an_empty_population_has_no_total(self) -> None:
        curve = qini_curve([])
        assert curve.n == 0
        assert curve.total == 0
        assert not curve.is_defined

    def test_the_random_line_reaches_the_total(self) -> None:
        curve = qini_curve(
            population(responders=100, non_responders=100, responder_uplift=1, other_uplift=0)
        )
        assert curve.random_at(0) == 0
        assert curve.random_at(curve.n) == curve.total

    def test_the_random_line_is_proportional(self) -> None:
        curve = qini_curve(
            population(responders=100, non_responders=100, responder_uplift=1, other_uplift=0)
        )
        assert curve.random_at(curve.n // 2) == curve.total // 2


class TestPerfectRanking:
    def test_it_scores_strongly_positive(self) -> None:
        """Every responder ranked above every non-responder: the curve reaches
        its total halfway and stays flat, well above the random line."""
        units = population(
            responders=250, non_responders=250, responder_uplift=10_000, other_uplift=0
        )
        result = evaluate(units)
        assert result.coefficient_bps is not None
        assert result.coefficient_bps > 2_000
        assert result.beats_random

    def test_the_curve_reaches_its_total_early(self) -> None:
        units = population(
            responders=250, non_responders=250, responder_uplift=10_000, other_uplift=0
        )
        curve = qini_curve(units)
        halfway = curve.n // 2
        assert curve.at(halfway) == curve.total
        assert curve.at(halfway) > curve.random_at(halfway)

    def test_it_beats_the_random_line_at_every_interior_point(self) -> None:
        units = population(
            responders=200, non_responders=200, responder_uplift=10_000, other_uplift=0
        )
        curve = qini_curve(units)
        for k in range(curve.n // 4, curve.n, 25):
            assert curve.at(k) >= curve.random_at(k), k


class TestReverseRanking:
    def test_it_scores_negative(self) -> None:
        """Responders ranked last. The curve stays flat then climbs — below the
        random line throughout, which is worse than chance and reported so."""
        units = population(
            responders=250, non_responders=250, responder_uplift=0, other_uplift=10_000
        )
        result = evaluate(units)
        assert result.coefficient_bps is not None
        assert result.coefficient_bps < 0
        assert not result.beats_random

    def test_it_mirrors_the_perfect_ranking(self) -> None:
        forward = evaluate(
            population(responders=250, non_responders=250, responder_uplift=10_000, other_uplift=0)
        )
        reverse = evaluate(
            population(responders=250, non_responders=250, responder_uplift=0, other_uplift=10_000)
        )
        assert forward.coefficient_bps is not None and reverse.coefficient_bps is not None
        assert forward.coefficient_bps > 0 > reverse.coefficient_bps
        assert abs(forward.coefficient_bps + reverse.coefficient_bps) < 1_500

    def test_the_top_share_captures_almost_nothing(self) -> None:
        units = population(
            responders=250, non_responders=250, responder_uplift=0, other_uplift=10_000
        )
        capture = evaluate(units).top
        assert capture.capture_bps is not None
        assert capture.capture_bps < 1_000


class TestTies:
    def test_identical_uplift_resolves_by_risk_id(self) -> None:
        units = [
            RankedUnit(rid(3), 500, True, True),
            RankedUnit(rid(1), 500, False, False),
            RankedUnit(rid(2), 500, True, False),
        ]
        arms = {u.risk_id: (u.is_treatment, u.recovered) for u in units}
        scores = [_score(u) for u in units]
        assert [u.risk_id for u in rank(arms, scores)] == [rid(1), rid(2), rid(3)]

    def test_input_order_does_not_change_the_ranking(self) -> None:
        """Whole cells tie at once, so sort stability would otherwise let row
        order decide the curve."""
        units = [RankedUnit(rid(index), 500, index % 2 == 0, index % 3 == 0) for index in range(50)]
        arms = {u.risk_id: (u.is_treatment, u.recovered) for u in units}

        forward = rank(arms, [_score(u) for u in units])
        backward = rank(arms, [_score(u) for u in reversed(units)])
        assert [u.risk_id for u in forward] == [u.risk_id for u in backward]

    def test_an_entirely_tied_population_scores_near_zero(self) -> None:
        """No information in the ranking means no advantage over random."""
        units = [
            RankedUnit(rid(index), 500, index % 2 == 0, index % 4 == 0) for index in range(400)
        ]
        result = evaluate(units)
        assert result.coefficient_bps is not None
        assert abs(result.coefficient_bps) < 1_500

    def test_higher_uplift_still_sorts_first(self) -> None:
        units = [
            RankedUnit(rid(1), 100, True, True),
            RankedUnit(rid(2), 900, True, True),
            RankedUnit(rid(3), 500, False, False),
        ]
        arms = {u.risk_id: (u.is_treatment, u.recovered) for u in units}
        ordered = rank(arms, [_score(u) for u in units])
        assert [u.uplift_bps for u in ordered] == [900, 500, 100]


class TestNegativeOverallEffect:
    def test_the_total_is_negative(self) -> None:
        """Treatment did harm: controls recover, treated do not."""
        units = population(
            responders=100,
            non_responders=100,
            responder_uplift=10_000,
            other_uplift=0,
            responder_treated_recovers=False,
            responder_holdout_recovers=True,
        )
        assert qini_curve(units).total < 0

    def test_the_coefficient_is_still_defined(self) -> None:
        units = population(
            responders=100,
            non_responders=100,
            responder_uplift=10_000,
            other_uplift=0,
            responder_treated_recovers=False,
            responder_holdout_recovers=True,
        )
        result = evaluate(units)
        assert result.is_defined
        assert result.coefficient_bps is not None

    def test_capture_is_a_share_of_the_negative_total(self) -> None:
        """Ranking the harmed cases first captures a positive *share* of a
        negative total. The sign of the share must not flip."""
        units = population(
            responders=100,
            non_responders=100,
            responder_uplift=10_000,
            other_uplift=0,
            responder_treated_recovers=False,
            responder_holdout_recovers=True,
        )
        capture = evaluate(units).top
        assert capture.total < 0
        assert capture.qini_at_k < 0
        assert capture.capture_bps is not None
        assert capture.capture_bps > 0


class TestUndefined:
    @staticmethod
    def flat_population() -> list[RankedUnit]:
        """Both arms recover at the same rate: no incremental recovery at all."""
        units: list[RankedUnit] = []
        for index in range(200):
            units.append(RankedUnit(rid(index * 2), 500, True, index % 2 == 0))
            units.append(RankedUnit(rid(index * 2 + 1), 500, False, index % 2 == 0))
        return units

    def test_the_total_is_zero(self) -> None:
        assert qini_curve(self.flat_population()).total == 0

    def test_the_coefficient_is_none_not_zero(self) -> None:
        """Zero would read as "ranked no better than chance", which is a
        different claim from "there was nothing to apportion"."""
        curve = qini_curve(self.flat_population())
        assert not curve.is_defined
        assert qini_coefficient_bps(curve) is None

    def test_the_capture_is_none_not_zero(self) -> None:
        capture = top_capture(qini_curve(self.flat_population()))
        assert capture.capture_bps is None
        assert not capture.is_defined

    def test_the_result_says_it_is_undefined(self) -> None:
        result = evaluate(self.flat_population())
        assert not result.is_defined
        assert not result.beats_random
        assert result.as_dict()["qini_coefficient_bps"] is None

    def test_an_empty_population_is_undefined(self) -> None:
        result = evaluate([])
        assert result.coefficient_bps is None
        assert result.top.capture_bps is None


class TestTopShareBoundary:
    def test_the_default_share_is_the_top_fifth(self) -> None:
        assert TOP_SHARE_BPS == 2_000

    def test_k_is_exactly_one_fifth_of_the_population(self) -> None:
        units = population(responders=500, non_responders=500, responder_uplift=1, other_uplift=0)
        capture = top_capture(qini_curve(units))
        assert capture.n == 2_000
        assert capture.k == 400

    def test_k_truncates_rather_than_rounds(self) -> None:
        """The top 20% of 999 units is 199. Taking 200 would overstate it."""
        units = [RankedUnit(rid(i), 500, i % 2 == 0, False) for i in range(999)]
        assert top_capture(qini_curve(units)).k == 199

    def test_the_share_is_configurable(self) -> None:
        units = population(responders=500, non_responders=500, responder_uplift=1, other_uplift=0)
        curve = qini_curve(units)
        assert top_capture(curve, share_bps=1_000).k == 200
        assert top_capture(curve, share_bps=5_000).k == 1_000

    def test_the_whole_population_captures_everything(self) -> None:
        units = population(
            responders=250, non_responders=250, responder_uplift=10_000, other_uplift=0
        )
        capture = top_capture(qini_curve(units), share_bps=BPS_SCALE)
        assert capture.k == capture.n
        assert capture.capture_bps == BPS_SCALE

    def test_a_perfect_ranking_captures_most_of_it_early(self) -> None:
        units = population(
            responders=250, non_responders=250, responder_uplift=10_000, other_uplift=0
        )
        capture = evaluate(units).top
        assert capture.capture_bps is not None
        assert capture.capture_bps > 3_000

    def test_an_impossible_share_is_refused(self) -> None:
        curve = qini_curve(
            population(responders=10, non_responders=10, responder_uplift=1, other_uplift=0)
        )
        for bad in (0, -1, BPS_SCALE + 1):
            with pytest.raises(QiniError, match="share_bps"):
                top_capture(curve, share_bps=bad)


class TestDeterminism:
    def test_the_curve_repeats_exactly(self) -> None:
        units = population(
            responders=200, non_responders=200, responder_uplift=10_000, other_uplift=0
        )
        assert qini_curve(units).values == qini_curve(units).values

    def test_the_result_repeats_exactly(self) -> None:
        units = population(
            responders=200, non_responders=200, responder_uplift=10_000, other_uplift=0
        )
        assert evaluate(units).as_dict() == evaluate(units).as_dict()

    def test_it_carries_no_float(self) -> None:
        units = population(
            responders=200, non_responders=200, responder_uplift=10_000, other_uplift=0
        )
        payload = evaluate(units).as_dict()

        def walk(value: object) -> None:
            assert not isinstance(value, float), value
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)

        walk(payload)


class TestRankingTheCompletePopulation:
    def test_a_scored_unit_with_no_arm_is_refused(self) -> None:
        """The denominator is the complete enrolled population; a unit that
        quietly vanished would flatter the curve."""
        units = [RankedUnit(rid(1), 500, True, True)]
        with pytest.raises(QiniError, match="complete population"):
            rank({}, [_score(u) for u in units])

    def test_every_scored_unit_appears_once(self) -> None:
        units = [RankedUnit(rid(i), 500 - i, i % 2 == 0, False) for i in range(100)]
        arms = {u.risk_id: (u.is_treatment, u.recovered) for u in units}
        ordered = rank(arms, [_score(u) for u in units])
        assert len(ordered) == 100
        assert len({u.risk_id for u in ordered}) == 100

    def test_the_arm_and_outcome_come_from_the_mapping(self) -> None:
        unit = RankedUnit(rid(1), 500, True, True)
        ordered = rank({rid(1): (False, True)}, [_score(unit)])
        assert ordered[0].is_treatment is False
        assert ordered[0].recovered is True

    def test_units_the_model_would_not_label_are_still_ranked(self) -> None:
        """A ranking evaluated only where the model was confident is evaluated
        on a sample it selected."""
        units = [RankedUnit(rid(i), 0, i % 2 == 0, False) for i in range(50)]
        arms = {u.risk_id: (u.is_treatment, u.recovered) for u in units}
        assert len(rank(arms, [_score(u, qualified=False) for u in units])) == 50


class TestPurity:
    @staticmethod
    def _tree() -> ast.Module:
        from app.causal import qini as module

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

    def test_it_assigns_no_quadrant(self) -> None:
        for banned in ("Quadrant", "GRAY_ZONE", "SLEEPING_DOG", "quadrant"):
            assert banned not in self._identifiers(), banned

    def test_it_persists_nothing(self) -> None:
        for module in self._imports():
            assert "models" not in module, module

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


def _score(unit: RankedUnit, *, qualified: bool = True):  # noqa: ANN202
    """An `UpliftScore` carrying just what the ranking reads."""
    from app.causal.estimators import Interval
    from app.causal.uplift import MODEL_VERSION, UpliftScore

    return UpliftScore(
        risk_id=unit.risk_id,
        model_version=MODEL_VERSION,
        fold=0,
        level=0 if qualified else None,
        level_name="failure_code|payment_method" if qualified else None,
        cell_key="cell" if qualified else "(global)",
        p_treat_bps=0,
        p_control_bps=0,
        uplift_bps=unit.uplift_bps,
        interval=Interval(low=0, high=0, alpha_bps=500, resamples=100, seed=1),
        harm_uplift_bps=0,
        qualified=qualified,
        reason="qualified" if qualified else "global_fallback",
        n_treated=1,
        n_holdout=1,
    )
