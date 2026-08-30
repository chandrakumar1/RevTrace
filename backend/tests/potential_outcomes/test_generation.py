"""Generation: determinism, assignment-independence, and the reveal boundary.

The two properties that make the benchmark meaningful:

* **Potential outcomes are drawn before, and independently of, any assignment.**
  If the arm influenced the draw, the "true" ATE would be an artefact of the
  randomiser rather than of the planted segments.
* **The reveal drops everything else.** `RevealedCase` is a separate type with
  no truth fields, so a pipeline handed one cannot read the answer key even by
  accident.
"""

from __future__ import annotations

import dataclasses

import pytest
from simulator.potential_outcomes import (
    DEFAULT_CASE_COUNT,
    POTENTIAL_OUTCOMES_VERSION,
    PotentialOutcomeCase,
    RevealedCase,
    _bernoulli,
    _rate_bps,
    generate,
    overall_truth,
    segment_truth,
    self_recovery_share_bps,
    truth_by_segment,
)
from simulator.rng import DeterministicRng
from simulator.segments import BPS_SCALE, CANDIDATE_ACTIONS, SEGMENTS, Action, SegmentId

SEED = 42
SMALL = 400


class TestDeterminism:
    def test_the_same_seed_reproduces_every_case(self) -> None:
        first = generate(seed=SEED, case_count=SMALL)
        second = generate(seed=SEED, case_count=SMALL)
        assert first.cases == second.cases

    def test_a_different_seed_produces_a_different_population(self) -> None:
        assert (
            generate(seed=SEED, case_count=SMALL).cases
            != generate(seed=SEED + 1, case_count=SMALL).cases
        )

    def test_case_ids_are_reproducible_and_unique(self) -> None:
        population = generate(seed=SEED, case_count=SMALL)
        ids = [case.case_id for case in population.cases]
        assert len(set(ids)) == SMALL
        assert ids == [c.case_id for c in generate(seed=SEED, case_count=SMALL).cases]

    def test_a_prefix_is_stable_as_the_population_grows(self) -> None:
        """Case n does not depend on how many cases follow it, because each
        draws from its own derived sub-stream."""
        short = generate(seed=SEED, case_count=50)
        long = generate(seed=SEED, case_count=500)
        assert short.cases == long.cases[:50]

    def test_the_version_is_recorded(self) -> None:
        assert generate(seed=SEED, case_count=10).generator_version == POTENTIAL_OUTCOMES_VERSION

    def test_the_v1_generator_version_was_not_bumped(self) -> None:
        """Bumping it would move every v1 checksum and break 1,587 tests."""
        from simulator.version import GENERATOR_VERSION

        assert GENERATOR_VERSION == "1.0.0"
        assert POTENTIAL_OUTCOMES_VERSION != GENERATOR_VERSION


class TestValidation:
    def test_case_count_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1):
            with pytest.raises(ValueError, match="at least 1"):
                generate(seed=SEED, case_count=bad)

    def test_a_float_case_count_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be an int"):
            generate(seed=SEED, case_count=10.0)  # type: ignore[arg-type]

    def test_a_negative_seed_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            generate(seed=-1, case_count=10)


class TestBernoulli:
    """Integer arithmetic only, and correct at both boundaries."""

    def test_zero_never_fires(self) -> None:
        rng = DeterministicRng(1)
        assert not any(_bernoulli(rng, 0) for _ in range(200))

    def test_full_scale_always_fires(self) -> None:
        rng = DeterministicRng(1)
        assert all(_bernoulli(rng, BPS_SCALE) for _ in range(200))

    def test_a_half_probability_lands_near_half(self) -> None:
        rng = DeterministicRng(9)
        hits = sum(1 for _ in range(20_000) if _bernoulli(rng, 5_000))
        assert 9_500 <= hits <= 10_500

    def test_negative_never_fires(self) -> None:
        assert not _bernoulli(DeterministicRng(1), -100)


class TestRateArithmetic:
    def test_zero_total_is_zero(self) -> None:
        assert _rate_bps(0, 0) == 0

    def test_exact_rates(self) -> None:
        assert _rate_bps(1, 2) == 5_000
        assert _rate_bps(3, 4) == 7_500
        assert _rate_bps(10, 10) == BPS_SCALE

    def test_it_rounds_half_up(self) -> None:
        assert _rate_bps(1, 3) == 3_333
        assert _rate_bps(2, 3) == 6_667

    def test_the_result_is_always_an_int(self) -> None:
        for hits, total in ((1, 7), (5, 9), (13, 17)):
            assert isinstance(_rate_bps(hits, total), int)


class TestAssignmentIndependence:
    """The property that makes the outcomes *potential* rather than realised."""

    def test_both_outcomes_are_drawn_for_every_case(self) -> None:
        for case in generate(seed=SEED, case_count=SMALL).cases:
            assert isinstance(case.y0, bool)
            for action in CANDIDATE_ACTIONS:
                assert isinstance(case.y1[action], bool)
                assert isinstance(case.harm1[action], bool)

    def test_no_arm_appears_anywhere_in_the_case(self) -> None:
        """Nothing in a generated case knows which arm it will be assigned.

        Checked against exact field names rather than substrings: `harm0` and
        `harm1` both contain "arm", and flagging them would be the guard
        misfiring on the harm metric it exists to protect.
        """
        fields = {f.name for f in dataclasses.fields(PotentialOutcomeCase)}
        assert fields.isdisjoint({"arm", "assignment", "treatment", "holdout"})

    def test_revealing_different_actions_does_not_change_the_case(self) -> None:
        case = generate(seed=SEED, case_count=1).cases[0]
        before = (case.y0, dict(case.y1), case.harm0, dict(case.harm1))
        for action in (Action.NO_ACTION, *CANDIDATE_ACTIONS):
            case.reveal(action)
        assert (case.y0, dict(case.y1), case.harm0, dict(case.harm1)) == before

    def test_the_generator_never_consults_an_arm(self) -> None:
        import ast
        import inspect
        import pathlib

        from simulator import potential_outcomes

        source = pathlib.Path(inspect.getfile(potential_outcomes)).read_text(encoding="utf-8")
        names: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        assert "arm" not in names
        assert "Arm" not in names


class TestReveal:
    def test_no_action_reveals_the_control_outcome(self) -> None:
        case = generate(seed=SEED, case_count=1).cases[0]
        assert case.reveal(Action.NO_ACTION).recovered == case.y0

    def test_an_action_reveals_that_actions_outcome(self) -> None:
        case = generate(seed=SEED, case_count=1).cases[0]
        for action in CANDIDATE_ACTIONS:
            assert case.reveal(action).recovered == case.y1[action]

    def test_a_revealed_case_carries_no_ground_truth(self) -> None:
        fields = {f.name for f in dataclasses.fields(RevealedCase)}
        for banned in ("y0", "y1", "harm0", "harm1", "segment_id", "probabilities"):
            assert banned not in fields, banned

    def test_a_revealed_case_exposes_no_truth_attribute_at_all(self) -> None:
        """`harmed` is deliberately present and is not ground truth: it is the
        realised harm under the action actually taken, which a real system does
        observe. What must be absent is the *other* arm's outcome."""
        revealed = generate(seed=SEED, case_count=1).cases[0].reveal(Action.RETRY_PAYMENT)
        for banned in (
            "y0",
            "y1",
            "harm0",
            "harm1",
            "segment_id",
            "probabilities",
            "true_uplift_bps",
            "true_harm_uplift_bps",
            "outcome_for",
            "harm_for",
        ):
            assert not hasattr(revealed, banned), banned

    def test_a_revealed_case_keeps_only_the_observed_harm(self) -> None:
        case = generate(seed=SEED, case_count=1).cases[0]
        assert case.reveal(Action.RETRY_PAYMENT).harmed == case.harm1[Action.RETRY_PAYMENT]
        assert case.reveal(Action.NO_ACTION).harmed == case.harm0

    def test_an_unrecovered_case_reveals_no_money(self) -> None:
        population = generate(seed=SEED, case_count=SMALL)
        for case in population.cases:
            revealed = case.reveal(Action.NO_ACTION)
            if not revealed.recovered:
                assert revealed.amount_minor == 0

    def test_a_recovered_case_reveals_its_amount(self) -> None:
        population = generate(seed=SEED, case_count=SMALL)
        recovered = [c for c in population.cases if c.y0]
        assert recovered
        for case in recovered:
            assert case.reveal(Action.NO_ACTION).amount_minor == case.amount_minor


class TestPopulationShape:
    def test_the_benchmark_size_is_within_the_planned_band(self) -> None:
        assert 8_000 <= DEFAULT_CASE_COUNT <= 12_000

    def test_every_segment_is_represented(self) -> None:
        population = generate(seed=SEED, case_count=DEFAULT_CASE_COUNT)
        for spec in SEGMENTS:
            assert len(population.by_segment(spec.id)) > 0, spec.id

    def test_segment_shares_track_their_planted_weights(self) -> None:
        population = generate(seed=SEED, case_count=DEFAULT_CASE_COUNT)
        for spec in SEGMENTS:
            share = _rate_bps(len(population.by_segment(spec.id)), len(population))
            assert abs(share - spec.weight_bps) <= 400, spec.id

    def test_amounts_are_integer_minor_units(self) -> None:
        for case in generate(seed=SEED, case_count=SMALL).cases:
            assert isinstance(case.amount_minor, int)
            assert not isinstance(case.amount_minor, bool)
            assert case.amount_minor > 0


@pytest.fixture(scope="module")
def large() -> object:
    """A population big enough for realised rates to settle."""
    return generate(seed=7, case_count=40_000)


@pytest.fixture(scope="module")
def benchmark() -> object:
    """The committed benchmark population."""
    return generate(seed=SEED, case_count=DEFAULT_CASE_COUNT)


class TestGroundTruthConvergence:
    """Realised rates must approach the planted parameters."""

    def test_segment_one_self_recovers_three_quarters_of_the_time(self, large) -> None:  # noqa: ANN001
        truth = segment_truth(large, SegmentId.TRANSIENT_UPI_TIMEOUT, Action.CREATE_PAYMENT_LINK)
        assert abs(truth.y0_rate_bps - 7_500) <= 250

    def test_segment_three_responds_only_to_the_link(self, large) -> None:  # noqa: ANN001
        link = segment_truth(large, SegmentId.EXPIRED_OR_BLOCKED_CARD, Action.CREATE_PAYMENT_LINK)
        retry = segment_truth(large, SegmentId.EXPIRED_OR_BLOCKED_CARD, Action.RETRY_PAYMENT)
        assert link.true_ate_bps > 5_000
        assert retry.true_ate_bps < 500

    def test_segment_five_stays_a_lost_cause(self, large) -> None:  # noqa: ANN001
        truth = segment_truth(large, SegmentId.INTENTIONAL_CHURNER, Action.CREATE_PAYMENT_LINK)
        assert truth.true_ate_bps < 500

    def test_segment_seven_shows_a_large_genuine_uplift(self, large) -> None:  # noqa: ANN001
        truth = segment_truth(large, SegmentId.HIGH_VALUE_CUSTOMER, Action.CREATE_PAYMENT_LINK)
        assert truth.true_ate_bps > 2_000


class TestSleepingDogIsRecoverable:
    """The Day 2 acceptance criterion."""

    def test_segment_six_shows_negative_uplift_on_the_harm_metric(self, benchmark) -> None:  # noqa: ANN001
        """Negative for the merchant: acting raises mandate cancellation."""
        truth = segment_truth(
            benchmark, SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER, Action.CREATE_PAYMENT_LINK
        )
        assert truth.true_harm_ate_bps >= 700, truth

    def test_its_recovery_uplift_is_negligible_or_negative(self, benchmark) -> None:  # noqa: ANN001
        truth = segment_truth(
            benchmark, SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER, Action.CREATE_PAYMENT_LINK
        )
        assert truth.true_ate_bps <= 200, truth

    def test_it_is_flagged_as_a_sleeping_dog(self, benchmark) -> None:  # noqa: ANN001
        truth = segment_truth(
            benchmark, SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER, Action.CREATE_PAYMENT_LINK
        )
        assert truth.is_sleeping_dog

    def test_no_other_segment_is_flagged(self, benchmark) -> None:  # noqa: ANN001
        flagged = [
            truth.segment_id
            for truth in truth_by_segment(benchmark, Action.CREATE_PAYMENT_LINK)
            if truth.is_sleeping_dog
        ]
        assert flagged == [SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER]

    def test_the_headline_self_recovery_share_is_substantial(self, benchmark) -> None:  # noqa: ANN001
        """Money a gross-recovery dashboard credits itself for."""
        assert self_recovery_share_bps(benchmark) >= 3_000

    def test_the_overall_effect_is_positive_despite_the_dog(self, benchmark) -> None:  # noqa: ANN001
        ate, harm_ate = overall_truth(benchmark, Action.CREATE_PAYMENT_LINK)
        assert ate > 0
        assert harm_ate > 0  # acting carries real, measurable harm in aggregate
