"""The T-learner, pinned on constructed populations.

Everything here is pure: units are built in memory, so the model's behaviour is
checked against arithmetic rather than against a database. The end-to-end run
over a materialised population belongs to a later gate.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid
from collections import Counter

import pytest

from app.causal.cells import COARSE, FINE, CellCounts, Features, Unit, fold_of
from app.causal.estimators import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, rate_effect
from app.causal.uplift import (
    GLOBAL_CELL,
    GLOBAL_FALLBACK,
    MODEL_VERSION,
    UpliftError,
    cross_fit,
    fit_fold,
    score,
    uplift_statistic,
)
from app.models.enums import Arm

ALPHA = 500
MDE = 1_000
FAST = 100  # resamples; above the 40 the percentile method needs at this alpha
EXPERIMENT_ID = uuid.UUID("eeeeeeee-0000-4000-8000-000000000005")


def features(failure_code: str = "gateway_timeout", payment_method: str = "upi") -> Features:
    return Features(
        failure_code=failure_code,
        payment_method=payment_method,
        amount_band="2000-5000",
        hour_bucket="morning",
        tenure_bucket="established",
        salary_window=False,
    )


def make_units(
    spec: dict[tuple[str, str], tuple[int, int, int, int]],
    *,
    harmed_treated: dict[tuple[str, str], int] | None = None,
) -> list[Unit]:
    """Units from a per-cell spec of (n_t, recovered_t, n_h, recovered_h).

    Ids are drawn from a fixed generator so folds are reproducible across runs
    of this file.
    """
    import random

    rng = random.Random(20260831)
    harmed_treated = harmed_treated or {}
    units: list[Unit] = []

    for (code, method), (n_t, r_t, n_h, r_h) in spec.items():
        harm_budget = harmed_treated.get((code, method), 0)
        for index in range(n_t):
            units.append(
                Unit(
                    risk_id=uuid.UUID(int=rng.getrandbits(128), version=4),
                    arm=Arm.TREATMENT.value,
                    recovered=index < r_t,
                    harmed=index < harm_budget,
                    features=features(code, method),
                )
            )
        for index in range(n_h):
            units.append(
                Unit(
                    risk_id=uuid.UUID(int=rng.getrandbits(128), version=4),
                    arm=Arm.HOLDOUT.value,
                    recovered=index < r_h,
                    harmed=False,
                    features=features(code, method),
                )
            )
    return units


#: A population where the fine cell comfortably qualifies in every fold.
BIG = {("gateway_timeout", "upi"): (5_000, 3_500, 5_000, 3_000)}


class TestTheStatistic:
    def test_it_is_the_difference_of_two_rounded_rates(self) -> None:
        assert uplift_statistic(45, 100, 35, 100) == 1_000

    def test_it_can_be_negative(self) -> None:
        assert uplift_statistic(30, 100, 40, 100) == -1_000

    def test_it_matches_the_population_estimator(self) -> None:
        """A cell interval and the overall interval must be the same
        construction at different scopes, not two conventions."""
        treatment = [1] * 45 + [0] * 55
        holdout = [1] * 35 + [0] * 65
        overall = rate_effect(treatment, holdout, alpha_bps=ALPHA, resamples=FAST)
        assert uplift_statistic(45, 100, 35, 100) == overall.ate_bps


class TestFittingOneFold:
    def test_it_trains_on_every_other_fold(self) -> None:
        units = make_units(BIG)
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        held_out = sum(1 for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0)
        assert model.training_size == len(units) - held_out
        assert held_out > 0

    def test_no_held_out_unit_is_in_the_training_counts(self) -> None:
        """The definition of cross-fitting, checked on the totals."""
        units = make_units(BIG)
        for fold in range(5):
            model = fit_fold(
                units, EXPERIMENT_ID, fold, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST
            )
            held = sum(1 for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == fold)
            assert model.training_size + held == len(units)

    def test_it_builds_one_tally_per_ladder_level(self) -> None:
        model = fit_fold(
            make_units(BIG), EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST
        )
        assert len(model.tallies) == 2
        assert len(model.harm_tallies) == 2

    def test_the_global_counts_cover_the_whole_training_set(self) -> None:
        model = fit_fold(
            make_units(BIG), EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST
        )
        assert model.global_counts.total == model.training_size

    def test_an_empty_population_is_refused(self) -> None:
        with pytest.raises(UpliftError, match="no training data"):
            fit_fold([], EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)


class TestTheCellRates:
    def test_the_rates_are_the_training_cell_rates(self) -> None:
        """70% treated recovery against 60% control: a 1000bps uplift."""
        units = make_units(BIG)
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        subject = next(u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0)

        result = score(subject, model)
        assert abs(result.p_treat_bps - 7_000) < 200
        assert abs(result.p_control_bps - 6_000) < 200
        assert result.uplift_bps == result.p_treat_bps - result.p_control_bps
        assert abs(result.uplift_bps - 1_000) < 300

    def test_a_negative_uplift_is_reported_as_such(self) -> None:
        """A treatment that made things worse is a result, not an error."""
        units = make_units({("mandate_inactive", "upi"): (4_000, 2_000, 4_000, 2_600)})
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        subject = next(u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0)

        result = score(subject, model)
        assert result.uplift_bps < 0

    def test_the_harm_uplift_is_estimated_separately(self) -> None:
        units = make_units(
            {("mandate_inactive", "upi"): (4_000, 2_600, 4_000, 2_600)},
            harmed_treated={("mandate_inactive", "upi"): 800},
        )
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        subject = next(u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0)

        result = score(subject, model)
        assert abs(result.uplift_bps) < 300
        assert result.harm_uplift_bps > 1_500

    def test_harm_and_recovery_do_not_contaminate_each_other(self) -> None:
        """The harm view reuses the same tally with `harmed` in the outcome
        position, so a bug swapping them would show up here."""
        units = make_units(BIG, harmed_treated={("gateway_timeout", "upi"): 0})
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        subject = next(u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0)

        result = score(subject, model)
        assert result.harm_uplift_bps == 0
        assert result.uplift_bps > 0


class TestTheLadder:
    def test_a_qualifying_fine_cell_is_used(self) -> None:
        units = make_units(BIG)
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        subject = next(u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0)

        result = score(subject, model)
        assert result.level == 0
        assert result.level_name == FINE
        assert result.cell_key == "gateway_timeout|upi"
        assert result.qualified

    def test_a_thin_fine_cell_backs_off_to_the_coarse_one(self) -> None:
        """One code, split across three methods so no method is large enough,
        but the code as a whole is."""
        units = make_units(
            {
                ("card_declined", "card"): (900, 200, 900, 150),
                ("card_declined", "upi"): (300, 70, 300, 50),
                ("card_declined", "netbanking"): (300, 70, 300, 50),
            }
        )
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        thin = next(
            u
            for u in units
            if fold_of(u.risk_id, EXPERIMENT_ID) == 0 and u.features.payment_method == "upi"
        )

        result = score(thin, model)
        assert result.level == 1
        assert result.level_name == COARSE
        assert result.cell_key == "card_declined"
        assert result.qualified

    def test_it_falls_to_the_global_rates_when_nothing_qualifies(self) -> None:
        units = make_units(
            {
                ("a", "upi"): (60, 20, 60, 15),
                ("b", "card"): (60, 20, 60, 15),
            }
        )
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        subject = next(u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0)

        result = score(subject, model)
        assert result.cell_key == GLOBAL_CELL
        assert result.is_global_fallback

    def test_the_global_fallback_is_not_qualifying(self) -> None:
        """An unconditional average is not a conditional estimate, and must not
        be dressed up as one. The quadrant layer will read this as Gray Zone."""
        units = make_units({("a", "upi"): (60, 20, 60, 15)})
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        subject = next(u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0)

        result = score(subject, model)
        assert not result.qualified
        assert result.reason == GLOBAL_FALLBACK
        assert result.level is None
        assert result.level_name is None

    def test_an_unseen_cell_falls_through_rather_than_failing(self) -> None:
        units = make_units(BIG)
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        stranger = Unit(
            risk_id=uuid.uuid4(),
            arm=Arm.TREATMENT.value,
            recovered=False,
            harmed=False,
            features=features("never_seen", "wallet"),
        )

        result = score(stranger, model)
        assert result.cell_key == GLOBAL_CELL
        assert not result.qualified


class TestIntervals:
    def test_every_score_carries_one(self) -> None:
        units = make_units(BIG)
        for result in cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST):
            assert result.ci_low_bps <= result.ci_high_bps

    def test_the_interval_uses_the_pre_registered_seed(self) -> None:
        units = make_units(BIG)
        result = cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)[0]
        assert result.interval.seed == BOOTSTRAP_SEED == 20_260_830
        assert result.interval.alpha_bps == ALPHA

    def test_the_default_resample_count_is_the_pre_registered_one(self) -> None:
        """The bootstrap is not quietly swapped for a cheaper interval."""
        assert BOOTSTRAP_RESAMPLES == 10_000
        source = pathlib.Path("app/causal/uplift.py").read_text(encoding="utf-8")
        assert "bootstrap_interval" in source
        assert "z_for_confidence" not in source

    def test_a_cell_is_bootstrapped_once_per_fold(self) -> None:
        """Ten thousand units in one cell must cost one bootstrap, not ten
        thousand."""
        units = make_units(BIG)
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        held = [u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0]

        for unit in held:
            score(unit, model)
        assert model.cells_bootstrapped == 1
        assert len(held) > 100

    def test_the_interval_brackets_the_estimate_here(self) -> None:
        """Not guaranteed by a percentile bootstrap, and `uplift_scores` has a
        CHECK that requires it — so the property is surfaced and watched."""
        units = make_units(BIG)
        for result in cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST):
            assert result.interval.contains(result.uplift_bps)

    def test_a_cell_is_resolved_once_per_fold(self) -> None:
        """Load-bearing, not cosmetic. Deciding a cell runs the power
        calculation, which bisects the fixed-point normal twice; doing that per
        unit rather than per cell took this file from 19 seconds to 331."""
        units = make_units(BIG)
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        held = [u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0]

        for unit in held:
            score(unit, model)
        assert model.cells_resolved == 1
        assert len(held) > 100

    def test_the_memoised_resolution_still_belongs_to_its_own_unit(self) -> None:
        """Caching by cell must not leak one unit's identity into another's."""
        units = make_units(BIG)
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        held = [u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0][:5]

        for unit in held:
            assert model.resolution_for(unit).risk_id == unit.risk_id
            assert score(unit, model).risk_id == unit.risk_id

    def test_memoising_does_not_change_any_answer(self) -> None:
        """The cache is an optimisation, so a cold model and a warm one must
        agree on every field."""
        units = make_units(BIG)
        cold = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        warm = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        held = [u for u in units if fold_of(u.risk_id, EXPERIMENT_ID) == 0][:20]

        for unit in held:
            warm.resolution_for(unit)  # warm the cache first
        assert [score(u, cold).as_dict() for u in held] == [score(u, warm).as_dict() for u in held]

    def test_a_reconstructed_arm_is_the_same_multiset(self) -> None:
        from app.causal.uplift import _arm_values

        assert sorted(_arm_values(3, 5)) == [0, 0, 1, 1, 1]
        assert sum(_arm_values(3, 5)) == 3

    def test_an_impossible_arm_is_refused(self) -> None:
        from app.causal.uplift import _arm_values

        with pytest.raises(UpliftError, match="cannot build an arm"):
            _arm_values(6, 5)


class TestCrossFitting:
    def test_every_unit_is_scored_exactly_once(self) -> None:
        units = make_units(BIG)
        scores = cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        assert len(scores) == len(units)
        assert len({s.risk_id for s in scores}) == len(units)

    def test_every_fold_is_represented(self) -> None:
        units = make_units(BIG)
        scores = cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        assert {s.fold for s in scores} == set(range(5))

    def test_a_unit_is_scored_by_its_own_fold_model(self) -> None:
        units = make_units(BIG)
        scores = cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        for result in scores:
            assert result.fold == fold_of(result.risk_id, EXPERIMENT_ID)

    def test_results_come_back_ordered_by_risk_id(self) -> None:
        """The ranking the Qini curve will read must be a function of the data,
        not of row order."""
        units = make_units(BIG)
        scores = cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        assert [s.risk_id for s in scores] == sorted(s.risk_id for s in scores)

    def test_it_is_reproducible(self) -> None:
        units = make_units(BIG)
        first = cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        second = cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        assert [s.as_dict() for s in first] == [s.as_dict() for s in second]

    def test_input_order_does_not_change_the_result(self) -> None:
        import random

        units = make_units(BIG)
        shuffled = list(units)
        random.Random(7).shuffle(shuffled)

        plain = cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        mixed = cross_fit(shuffled, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        assert [s.as_dict() for s in plain] == [s.as_dict() for s in mixed]

    def test_nothing_in_nothing_out(self) -> None:
        assert cross_fit([], EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE) == []

    def test_too_few_folds_are_refused(self) -> None:
        with pytest.raises(UpliftError, match="at least two folds"):
            cross_fit(make_units(BIG), EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, folds=1)

    def test_the_model_version_is_stamped_on_every_score(self) -> None:
        units = make_units(BIG)
        scores = cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        assert {s.model_version for s in scores} == {MODEL_VERSION}
        assert len(MODEL_VERSION) <= 64  # the column is String(64)


class TestIntentionToTreat:
    def test_the_arm_is_taken_as_stored(self) -> None:
        """A treated unit stays treated whatever happened to it. This module
        never sees `execution_failed` — it is not on `Unit` at all."""
        assert not hasattr(
            Unit(
                risk_id=uuid.uuid4(),
                arm=Arm.TREATMENT.value,
                recovered=False,
                harmed=False,
                features=features(),
            ),
            "execution_failed",
        )

    def test_both_arms_reach_the_counts(self) -> None:
        units = make_units(BIG)
        model = fit_fold(units, EXPERIMENT_ID, 0, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        cell = model.tallies[0]["gateway_timeout|upi"]
        assert cell.n_treated > 0
        assert cell.n_holdout > 0

    def test_arm_proportions_survive_into_every_fold(self) -> None:
        units = make_units(BIG)
        for fold in range(5):
            model = fit_fold(
                units, EXPERIMENT_ID, fold, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST
            )
            counts: CellCounts = model.global_counts
            assert counts.n_treated > 0 and counts.n_holdout > 0


class TestSerialisation:
    def test_the_payload_carries_no_float(self) -> None:
        units = make_units(BIG)
        for result in cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST):
            for value in result.as_dict().values():
                assert not isinstance(value, float), value

    def test_it_carries_the_fields_persistence_will_need(self) -> None:
        units = make_units(BIG)
        payload = cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)[
            0
        ].as_dict()
        for column in (
            "risk_id",
            "model_version",
            "p_treat_bps",
            "p_control_bps",
            "uplift_bps",
            "uplift_ci_low_bps",
            "uplift_ci_high_bps",
        ):
            assert column in payload, column

    def test_it_carries_no_quadrant(self) -> None:
        """Assigning one is a later gate's job."""
        units = make_units(BIG)
        payload = cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)[
            0
        ].as_dict()
        assert "quadrant" not in payload

    def test_the_rates_stay_in_range(self) -> None:
        """`uplift_scores` constrains both to 0..10000 and the uplift to +/-."""
        units = make_units(BIG)
        for result in cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST):
            assert 0 <= result.p_treat_bps <= 10_000
            assert 0 <= result.p_control_bps <= 10_000
            assert -10_000 <= result.uplift_bps <= 10_000

    def test_the_reasons_are_a_small_closed_set(self) -> None:
        units = make_units(BIG)
        reasons = Counter(
            s.reason
            for s in cross_fit(units, EXPERIMENT_ID, alpha_bps=ALPHA, mde_bps=MDE, resamples=FAST)
        )
        assert set(reasons) <= {"qualified", GLOBAL_FALLBACK}


class TestPurity:
    """The model reads observables. It never sees the answer key."""

    @staticmethod
    def _tree() -> ast.Module:
        from app.causal import uplift as module

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
        identifiers = self._identifiers()
        for banned in ("y0", "y1", "harm0", "harm1", "segment_id", "truth_segment"):
            assert banned not in identifiers, banned

    def test_it_never_reads_execution_state(self) -> None:
        """Intention-to-treat: the arm is what was assigned, full stop."""
        assert "execution_failed" not in self._identifiers()

    def test_it_imports_no_generator_module(self) -> None:
        for module in self._imports():
            assert not module.startswith("simulator"), module

    def test_it_writes_nothing(self) -> None:
        identifiers = self._identifiers()
        for banned in ("add", "add_all", "commit", "flush", "merge", "delete"):
            assert banned not in identifiers, banned

    def test_it_persists_no_score(self) -> None:
        """`uplift_scores` rows are a later gate."""
        identifiers = self._identifiers()
        assert "UpliftScore" in identifiers  # the dataclass, not the model
        for module in self._imports():
            assert module != "app.models.uplift_score", module

    def test_it_assigns_no_quadrant(self) -> None:
        identifiers = self._identifiers()
        for banned in ("Quadrant", "GRAY_ZONE", "SLEEPING_DOG", "PERSUADABLE", "quadrant"):
            assert banned not in identifiers, banned

    def test_it_computes_no_qini(self) -> None:
        identifiers = self._identifiers()
        for banned in ("qini", "qini_bps", "capture_bps"):
            assert banned not in identifiers, banned

    def test_no_float_constant_appears(self) -> None:
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), node.value

    def test_there_is_no_true_division(self) -> None:
        for node in ast.walk(self._tree()):
            assert not isinstance(node, ast.Div), ast.dump(node)

    def test_it_reads_no_clock_and_draws_no_randomness(self) -> None:
        for banned in ("now", "utcnow", "today", "random"):
            assert banned not in self._identifiers(), banned
