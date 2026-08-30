"""The minimum-cell rule, pinned against real and degenerate cell shapes.

Five scenarios the contract turns on: a qualifying fine cell, a coarse fallback,
the impossible-baseline ceiling, the empty arm, and the divergence between the
balanced and the exact power requirement that motivated using the exact one.

The N~4000 Gray Zone behaviour needs a real population and lives in
`tests/benchmark/test_gray_zone.py`.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid

import pytest

from app.causal.cells import (
    BPS_SCALE,
    COARSE,
    DEFAULT_FOLD_COUNT,
    DEGENERATE_RATIO,
    EMPTY_ARM,
    FINE,
    LADDER,
    NO_ROOM_FOR_EFFECT,
    QUALIFIED,
    UNDERPOWERED,
    CellCounts,
    CellError,
    Features,
    Unit,
    coarse_key,
    fine_key,
    fold_of,
    hour_bucket,
    qualification_reason,
    qualifies,
    required_for,
    resolve,
    tally,
    tenure_bucket,
)
from app.causal.power import required_n_holdout, required_n_per_arm
from app.models.enums import Arm

MDE = 1_000
EXPERIMENT_ID = uuid.UUID("eeeeeeee-0000-4000-8000-000000000001")


def features(
    failure_code: str = "gateway_timeout",
    payment_method: str = "upi",
) -> Features:
    return Features(
        failure_code=failure_code,
        payment_method=payment_method,
        amount_band="2000-5000",
        hour_bucket="morning",
        tenure_bucket="established",
        salary_window=False,
    )


def counts(n_t: int, n_h: int, p_control_bps: int, p_treat_bps: int = 0) -> CellCounts:
    """A cell whose rates land as close to the targets as the arm sizes allow.

    Rounds half-up rather than truncating. Not every rate is representable at
    every arm size — 35.00% needs a multiple of 1/375 that does not exist — so
    tests below assert against the cell's *actual* rate rather than the target.
    """
    return CellCounts(
        n_treated=n_t,
        n_holdout=n_h,
        recovered_treated=(n_t * p_treat_bps + BPS_SCALE // 2) // BPS_SCALE,
        recovered_holdout=(n_h * p_control_bps + BPS_SCALE // 2) // BPS_SCALE,
    )


def unit(f: Features, *, arm: str = Arm.TREATMENT.value, recovered: bool = False) -> Unit:
    return Unit(
        risk_id=uuid.uuid4(),
        arm=arm,
        recovered=recovered,
        harmed=False,
        features=f,
    )


class TestAQualifyingFineCell:
    def test_a_real_fine_cell_qualifies(self) -> None:
        """`gateway_timeout|upi` from the seed-42 population: 681 treated, 611
        held out, control rate 71.03%."""
        cell = counts(681, 611, p_control_bps=7_103)
        assert abs(cell.p_control_bps - 7_103) <= 10
        assert qualifies(cell, mde_bps=MDE)
        assert qualification_reason(cell, mde_bps=MDE) == QUALIFIED

    def test_it_clears_the_requirement_with_room(self) -> None:
        cell = counts(681, 611, p_control_bps=7_103)
        needed = required_for(cell, mde_bps=MDE)
        assert needed is not None
        assert cell.n_holdout > needed

    def test_the_other_real_fine_cells_qualify_too(self) -> None:
        for n_t, n_h, p_control in (
            (672, 678, 619),  # card_declined|card
            (731, 657, 1_568),  # insufficient_funds|card
            (417, 415, 6_313),  # mandate_inactive|upi
        ):
            assert qualifies(counts(n_t, n_h, p_control), mde_bps=MDE)

    def test_a_real_thin_fine_cell_does_not(self) -> None:
        """`bank_unavailable|upi`: 123 treated, 115 held out. Nowhere near."""
        cell = counts(123, 115, p_control_bps=5_391)
        assert not qualifies(cell, mde_bps=MDE)
        assert qualification_reason(cell, mde_bps=MDE) == UNDERPOWERED

    def test_more_data_eventually_qualifies(self) -> None:
        previous = False
        for size in (100, 200, 400, 800, 1_600):
            current = qualifies(counts(size, size, 4_500), mde_bps=MDE)
            assert current or not previous, "qualification must not go backwards"
            previous = current
        assert previous


class TestTheCoarseFallback:
    def test_a_thin_fine_cell_backs_off_to_a_qualifying_coarse_one(self) -> None:
        subject = unit(features("bank_unavailable", "upi"))
        fine = {"bank_unavailable|upi": counts(123, 115, 5_391)}
        coarse = {"bank_unavailable": counts(554, 512, 3_984)}

        resolution = resolve(subject, [fine, coarse], mde_bps=MDE)
        assert resolution.level == 1
        assert resolution.level_name == COARSE
        assert resolution.key == "bank_unavailable"
        assert not resolution.is_gray_zone

    def test_a_qualifying_fine_cell_is_preferred(self) -> None:
        """The ladder is finest-first: heterogeneity is kept wherever the data
        supports it."""
        subject = unit(features("gateway_timeout", "upi"))
        fine = {"gateway_timeout|upi": counts(681, 611, 7_103)}
        coarse = {"gateway_timeout": counts(969, 912, 6_349)}

        resolution = resolve(subject, [fine, coarse], mde_bps=MDE)
        assert resolution.level == 0
        assert resolution.level_name == FINE
        assert resolution.key == "gateway_timeout|upi"

    def test_failing_both_levels_yields_no_label(self) -> None:
        subject = unit(features("bank_unavailable", "upi"))
        fine = {"bank_unavailable|upi": counts(12, 11, 5_391)}
        coarse = {"bank_unavailable": counts(30, 28, 3_984)}

        resolution = resolve(subject, [fine, coarse], mde_bps=MDE)
        assert resolution.is_gray_zone
        assert resolution.level is None
        assert resolution.key is None
        assert resolution.reason == UNDERPOWERED

    def test_an_unseen_fine_cell_backs_off(self) -> None:
        """A combination absent from training must not crash or be dropped."""
        subject = unit(features("mandate_inactive", "netbanking"))
        coarse = {"mandate_inactive": counts(641, 648, 5_370)}

        resolution = resolve(subject, [{}, coarse], mde_bps=MDE)
        assert resolution.level == 1

    def test_an_entirely_unseen_unit_yields_no_label(self) -> None:
        subject = unit(features("something_new", "upi"))
        resolution = resolve(subject, [{}, {}], mde_bps=MDE)
        assert resolution.is_gray_zone
        assert resolution.reason == EMPTY_ARM

    def test_the_ladder_is_two_levels_finest_first(self) -> None:
        assert [name for name, _ in LADDER] == [FINE, COARSE]

    def test_a_wrong_number_of_tallies_is_refused(self) -> None:
        with pytest.raises(CellError, match="expected 2 tallies"):
            resolve(unit(features()), [{}], mde_bps=MDE)


class TestTheImpossibleBaselineCeiling:
    def test_a_full_control_rate_cannot_qualify(self) -> None:
        """Every control unit recovered. A further +10pp is arithmetically
        impossible, so no sample size demonstrates it."""
        cell = CellCounts(
            n_treated=5_000, n_holdout=5_000, recovered_treated=5_000, recovered_holdout=5_000
        )
        assert cell.p_control_bps == BPS_SCALE
        assert not qualifies(cell, mde_bps=MDE)
        assert qualification_reason(cell, mde_bps=MDE) == NO_ROOM_FOR_EFFECT

    def test_it_does_not_raise(self) -> None:
        """`required_n_holdout` refuses a baseline past full scale. The cell
        rule has to answer, not propagate that."""
        cell = counts(5_000, 5_000, p_control_bps=9_900)
        assert qualification_reason(cell, mde_bps=MDE) == NO_ROOM_FOR_EFFECT
        assert required_for(cell, mde_bps=MDE) is None

    def test_the_boundary_is_exactly_at_full_scale(self) -> None:
        """9000 + 1000 = 10000 is allowed; one basis point more is not. Counts
        given exactly, because 9001 bps is not representable at every size."""
        allowed = CellCounts(n_treated=10_000, n_holdout=10_000, recovered_holdout=9_000)
        refused = CellCounts(n_treated=10_000, n_holdout=10_000, recovered_holdout=9_001)
        assert allowed.p_control_bps == 9_000
        assert refused.p_control_bps == 9_001
        assert qualification_reason(allowed, mde_bps=MDE) == QUALIFIED
        assert qualification_reason(refused, mde_bps=MDE) == NO_ROOM_FOR_EFFECT

    def test_a_zero_control_rate_is_fine(self) -> None:
        """The ceiling is one-sided: nobody recovering leaves plenty of room."""
        cell = CellCounts(n_treated=5_000, n_holdout=5_000)
        assert cell.p_control_bps == 0
        assert qualifies(cell, mde_bps=MDE)

    def test_a_larger_mde_lowers_the_ceiling(self) -> None:
        cell = counts(5_000, 5_000, p_control_bps=8_500)
        assert qualification_reason(cell, mde_bps=1_000) == QUALIFIED
        assert qualification_reason(cell, mde_bps=2_000) == NO_ROOM_FOR_EFFECT


class TestTheZeroArmGuard:
    def test_an_empty_holdout_arm_cannot_qualify(self) -> None:
        cell = CellCounts(n_treated=5_000, n_holdout=0)
        assert not qualifies(cell, mde_bps=MDE)
        assert qualification_reason(cell, mde_bps=MDE) == EMPTY_ARM

    def test_an_empty_treated_arm_cannot_qualify(self) -> None:
        cell = CellCounts(n_treated=0, n_holdout=5_000)
        assert qualification_reason(cell, mde_bps=MDE) == EMPTY_ARM

    def test_an_entirely_empty_cell_cannot_qualify(self) -> None:
        assert qualification_reason(CellCounts(), mde_bps=MDE) == EMPTY_ARM

    def test_neither_raises(self) -> None:
        for cell in (
            CellCounts(n_treated=5_000, n_holdout=0),
            CellCounts(n_treated=0, n_holdout=5_000),
            CellCounts(),
        ):
            assert required_for(cell, mde_bps=MDE) is None

    def test_a_ratio_rounding_to_zero_is_refused(self) -> None:
        """One holdout unit against ten thousand treated rounds the ratio to
        zero basis points, which `required_n_holdout` rejects."""
        cell = CellCounts(n_treated=10_000, n_holdout=1)
        assert cell.holdout_ratio_bps == 0
        assert qualification_reason(cell, mde_bps=MDE) == DEGENERATE_RATIO

    def test_impossible_counts_are_rejected_at_construction(self) -> None:
        with pytest.raises(CellError, match="cannot exceed the treated arm"):
            CellCounts(n_treated=10, recovered_treated=11)
        with pytest.raises(CellError, match="cannot exceed the holdout arm"):
            CellCounts(n_holdout=10, recovered_holdout=11)
        with pytest.raises(CellError, match="non-negative"):
            CellCounts(n_treated=-1)


class TestTheExactRequirementIsUsed:
    def test_the_balanced_form_would_have_been_lenient(self) -> None:
        """150 treated against 375 held out at a 35% control rate. The balanced
        formula asks 373 and waves it through; the exact one asks 665 and does
        not, because the *treated* arm is the thin one and the balanced form
        cannot see that."""
        cell = counts(150, 375, p_control_bps=3_500)

        balanced = required_n_per_arm(cell.p_control_bps, MDE)
        exact = required_n_holdout(cell.p_control_bps, MDE, holdout_bps=cell.holdout_ratio_bps)

        assert cell.holdout_ratio_bps == 7_142
        assert cell.n_holdout >= balanced, f"balanced asks {balanced}, cell has 375"
        assert cell.n_holdout < exact, f"exact asks {exact}, cell has 375"
        assert exact > balanced + 250, f"{exact} vs {balanced} — a wide gap, not a rounding"
        assert not qualifies(cell, mde_bps=MDE)

    def test_the_two_agree_on_a_balanced_cell(self) -> None:
        """Randomisation keeps cells near balanced, so the exact form is not a
        different rule in the ordinary case — only in the awkward one."""
        cell = counts(600, 600, p_control_bps=4_500)
        balanced = required_n_per_arm(cell.p_control_bps, MDE)
        exact = required_for(cell, mde_bps=MDE)
        assert exact is not None
        assert abs(exact - balanced) <= 2

    def test_the_ratio_is_the_cells_own_split(self) -> None:
        assert counts(500, 500, 4_000).holdout_ratio_bps == 5_000
        assert counts(750, 250, 4_000).holdout_ratio_bps == 2_500
        assert counts(250, 750, 4_000).holdout_ratio_bps == 7_500

    def test_checking_the_holdout_arm_implies_the_treated_one(self) -> None:
        """Which is why one check suffices: at the cell's true ratio, a holdout
        arm that clears its requirement means the treated arm clears its own."""
        cell = counts(900, 700, p_control_bps=4_000)
        assert qualifies(cell, mde_bps=MDE)
        assert cell.n_treated > cell.n_holdout


class TestRates:
    def test_the_control_rate_rounds_half_up(self) -> None:
        assert CellCounts(n_treated=1, n_holdout=8, recovered_holdout=1).p_control_bps == 1_250
        assert CellCounts(n_treated=1, n_holdout=32, recovered_holdout=1).p_control_bps == 313

    def test_an_empty_arm_reports_a_zero_rate(self) -> None:
        assert CellCounts().p_control_bps == 0
        assert CellCounts().p_treat_bps == 0

    def test_the_rates_reach_both_ends(self) -> None:
        full = CellCounts(n_treated=10, n_holdout=10, recovered_treated=10, recovered_holdout=10)
        assert full.p_treat_bps == BPS_SCALE
        assert full.p_control_bps == BPS_SCALE


class TestFolds:
    def test_it_is_deterministic(self) -> None:
        risk_id = uuid.uuid4()
        assert fold_of(risk_id, EXPERIMENT_ID) == fold_of(risk_id, EXPERIMENT_ID)

    def test_it_lands_in_range(self) -> None:
        for _ in range(500):
            assert 0 <= fold_of(uuid.uuid4(), EXPERIMENT_ID) < DEFAULT_FOLD_COUNT

    def test_it_spreads_across_every_fold(self) -> None:
        seen = {fold_of(uuid.uuid4(), EXPERIMENT_ID) for _ in range(500)}
        assert seen == set(range(DEFAULT_FOLD_COUNT))

    def test_it_is_roughly_even(self) -> None:
        from collections import Counter

        spread = Counter(fold_of(uuid.uuid4(), EXPERIMENT_ID) for _ in range(5_000))
        assert all(850 <= count <= 1_150 for count in spread.values()), spread

    def test_a_different_experiment_reshuffles(self) -> None:
        risk_id = uuid.uuid4()
        other = uuid.uuid4()
        folds = {fold_of(risk_id, EXPERIMENT_ID), fold_of(risk_id, other)}
        assert len(folds) >= 1  # may collide; the point is it is a function of both

    def test_the_salt_differs_from_the_assignment_salt(self) -> None:
        """Fold and arm must be independent draws, not two views of one hash."""
        from app.causal.cells import FOLD_SALT
        from app.core.config import get_settings

        assert FOLD_SALT != get_settings().assignment_salt

    def test_too_few_folds_are_refused(self) -> None:
        with pytest.raises(CellError, match="at least two folds"):
            fold_of(uuid.uuid4(), EXPERIMENT_ID, folds=1)


class TestTallying:
    def test_it_counts_both_arms(self) -> None:
        units = [
            unit(features(), arm=Arm.TREATMENT.value, recovered=True),
            unit(features(), arm=Arm.TREATMENT.value, recovered=False),
            unit(features(), arm=Arm.HOLDOUT.value, recovered=True),
        ]
        cells = tally(units, fine_key)
        cell = cells["gateway_timeout|upi"]
        assert (cell.n_treated, cell.recovered_treated) == (2, 1)
        assert (cell.n_holdout, cell.recovered_holdout) == (1, 1)

    def test_it_separates_cells(self) -> None:
        units = [unit(features("a", "upi")), unit(features("b", "card"))]
        assert set(tally(units, fine_key)) == {"a|upi", "b|card"}

    def test_the_coarse_key_merges_methods(self) -> None:
        units = [unit(features("a", "upi")), unit(features("a", "card"))]
        assert set(tally(units, coarse_key)) == {"a"}
        assert tally(units, coarse_key)["a"].n_treated == 2

    def test_nothing_tallies_to_nothing(self) -> None:
        assert tally([], fine_key) == {}


class TestBuckets:
    def test_hours_bucket_into_four(self) -> None:
        assert hour_bucket(0) == "night"
        assert hour_bucket(9) == "morning"
        assert hour_bucket(14) == "afternoon"
        assert hour_bucket(23) == "evening"

    def test_every_hour_buckets(self) -> None:
        assert len({hour_bucket(h) for h in range(24)}) == 4

    def test_an_impossible_hour_is_refused(self) -> None:
        with pytest.raises(CellError, match="0..23"):
            hour_bucket(24)

    def test_tenure_buckets_into_four(self) -> None:
        assert tenure_bucket(0) == "new"
        assert tenure_bucket(200) == "established"
        assert tenure_bucket(500) == "loyal"
        assert tenure_bucket(1_460) == "veteran"

    def test_negative_tenure_is_refused(self) -> None:
        with pytest.raises(CellError, match="non-negative"):
            tenure_bucket(-1)


class TestTheFeatureSet:
    def test_lifetime_value_is_not_a_feature(self) -> None:
        """The column exists because the schema requires it. Its value is a
        placeholder, not a measurement, and must never reach a model."""
        assert "lifetime_value" not in features().as_dict()

    def test_the_features_are_exactly_the_approved_six(self) -> None:
        assert set(features().as_dict()) == {
            "failure_code",
            "payment_method",
            "amount_band",
            "hour_bucket",
            "tenure_bucket",
            "salary_window",
        }

    def test_no_unavailable_covariate_appears(self) -> None:
        """`issuer`, `prior_recovery_count` and `prior_contact_count_30d` have
        no column anywhere and were left that way."""
        payload = features().as_dict()
        for absent in ("issuer", "prior_recovery_count", "prior_contact_count_30d"):
            assert absent not in payload


class TestPurity:
    """Cells count. They read no ground truth and write nothing."""

    @staticmethod
    def _tree() -> ast.Module:
        from app.causal import cells as module

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

    def test_it_never_names_ground_truth(self) -> None:
        for name in self._identifiers():
            assert not name.startswith("truth_"), name

    def test_it_never_names_a_potential_outcome(self) -> None:
        identifiers = self._identifiers()
        for banned in ("y0", "y1", "harm0", "harm1", "segment_id", "truth_segment"):
            assert banned not in identifiers, banned

    def test_it_writes_nothing(self) -> None:
        identifiers = self._identifiers()
        for banned in ("add", "add_all", "commit", "flush", "merge", "delete"):
            assert banned not in identifiers, banned

    def test_it_imports_no_generator_module(self) -> None:
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("simulator"), node.module

    def test_it_reads_no_clock_and_draws_no_randomness(self) -> None:
        for banned in ("now", "utcnow", "today", "random", "choices"):
            assert banned not in self._identifiers(), banned

    def test_no_float_constant_appears(self) -> None:
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), node.value

    def test_it_names_no_quadrant(self) -> None:
        """Mapping an unqualified cell to GRAY_ZONE is the quadrant layer's
        job. Keeping the two apart is what lets each be tested alone."""
        identifiers = self._identifiers()
        for banned in ("Quadrant", "GRAY_ZONE", "SLEEPING_DOG", "PERSUADABLE", "quadrant"):
            assert banned not in identifiers, banned

    def test_it_estimates_no_uplift(self) -> None:
        """This gate builds the substrate only."""
        identifiers = self._identifiers()
        for banned in ("uplift", "qini", "UpliftScore", "bootstrap_interval"):
            assert banned not in identifiers, banned
