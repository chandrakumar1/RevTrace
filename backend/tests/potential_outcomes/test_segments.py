"""The seven planted segments: parameters, mix, and the properties they encode.

Each segment exists to make one thing measurable. These tests assert that the
thing is actually there — a segment whose planted effect had been mistyped would
still generate data, still produce an ATE, and still look fine.
"""

from __future__ import annotations

import pytest
from simulator.segments import (
    BPS_SCALE,
    CANDIDATE_ACTIONS,
    SEGMENTS,
    SEGMENTS_BY_ID,
    Action,
    Covariates,
    OutcomeProbabilities,
    SegmentId,
    segment_for_draw,
    total_weight_bps,
)


def covariates(**overrides: object) -> Covariates:
    fields: dict[str, object] = {
        "salary_window": False,
        "downtime_ended": False,
        "day_of_month": 12,
        "amount_minor": 230_400,
        "payment_method": "card",
        "issuer": "hdfc",
        "tenure_days": 200,
        "prior_recovery_count": 0,
        "prior_contact_count_30d": 0,
        "hour_of_day": 14,
    }
    fields.update(overrides)
    return Covariates(**fields)  # type: ignore[arg-type]


class TestTheMix:
    def test_there_are_exactly_seven(self) -> None:
        assert len(SEGMENTS) == 7
        assert len(SegmentId) == 7

    def test_weights_sum_to_full_scale(self) -> None:
        assert total_weight_bps() == BPS_SCALE

    def test_every_segment_has_mass(self) -> None:
        """A segment with no cases proves nothing."""
        for spec in SEGMENTS:
            assert spec.weight_bps > 0, spec.id

    def test_the_sleeping_dog_has_enough_mass_to_be_detectable(self) -> None:
        """An undetectable planted effect would demonstrate nothing on Day 5."""
        spec = SEGMENTS_BY_ID[SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER]
        assert spec.weight_bps >= 1_000

    def test_ids_are_unique(self) -> None:
        assert len({spec.id for spec in SEGMENTS}) == 7

    def test_every_segment_documents_why_it_exists(self) -> None:
        for spec in SEGMENTS:
            assert spec.rationale.strip()
            assert spec.label.strip()


class TestDraw:
    def test_the_first_and_last_draw_land_inside_the_mix(self) -> None:
        assert segment_for_draw(1) is SEGMENTS[0]
        assert segment_for_draw(BPS_SCALE) is SEGMENTS[-1]

    def test_every_draw_resolves(self) -> None:
        for draw in range(1, BPS_SCALE + 1, 37):
            assert segment_for_draw(draw) in SEGMENTS

    def test_out_of_range_is_rejected(self) -> None:
        for bad in (0, -1, BPS_SCALE + 1):
            with pytest.raises(ValueError, match="draw_bps"):
                segment_for_draw(bad)

    def test_boundaries_partition_cleanly(self) -> None:
        cumulative = 0
        for spec in SEGMENTS:
            cumulative += spec.weight_bps
            assert segment_for_draw(cumulative) is spec


class TestParametersAreIntegers:
    def test_no_probability_is_a_float(self) -> None:
        """Floats would make the arithmetic non-reproducible (ADR 0001)."""
        for spec in SEGMENTS:
            probabilities = spec.resolve(covariates())
            values = [probabilities.y0_bps, probabilities.harm0_bps]
            values += list(probabilities.y1_bps.values())
            values += list(probabilities.harm1_bps.values())
            for value in values:
                assert isinstance(value, int) and not isinstance(value, bool), spec.id

    def test_every_probability_is_within_scale(self) -> None:
        for spec in SEGMENTS:
            for flags in ({"salary_window": True}, {"downtime_ended": True}, {}):
                probabilities = spec.resolve(covariates(**flags))
                assert 0 <= probabilities.y0_bps <= BPS_SCALE
                for value in probabilities.y1_bps.values():
                    assert 0 <= value <= BPS_SCALE

    def test_every_candidate_action_is_covered(self) -> None:
        for spec in SEGMENTS:
            probabilities = spec.resolve(covariates())
            assert set(probabilities.y1_bps) >= set(CANDIDATE_ACTIONS), spec.id
            assert set(probabilities.harm1_bps) >= set(CANDIDATE_ACTIONS), spec.id

    def test_a_missing_action_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="every candidate action"):
            OutcomeProbabilities(
                y0_bps=1_000,
                y1_bps={Action.RETRY_PAYMENT: 2_000},
                harm0_bps=0,
                harm1_bps={Action.RETRY_PAYMENT: 0, Action.CREATE_PAYMENT_LINK: 0},
            )

    def test_a_float_probability_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="basis points"):
            OutcomeProbabilities(
                y0_bps=0.75,  # type: ignore[arg-type]
                y1_bps=dict.fromkeys(CANDIDATE_ACTIONS, 7_800),
                harm0_bps=0,
                harm1_bps=dict.fromkeys(CANDIDATE_ACTIONS, 0),
            )


class TestSegment1SelfRecovery:
    """The source of the industry's inflated gross-recovery number."""

    def test_three_quarters_would_pay_unprompted(self) -> None:
        probabilities = SEGMENTS_BY_ID[SegmentId.TRANSIENT_UPI_TIMEOUT].resolve(covariates())
        assert probabilities.y0_bps == 7_500

    def test_acting_barely_moves_it(self) -> None:
        probabilities = SEGMENTS_BY_ID[SegmentId.TRANSIENT_UPI_TIMEOUT].resolve(covariates())
        for action in CANDIDATE_ACTIONS:
            assert probabilities.uplift_bps(action) < 500


class TestSegment2Timing:
    """The treatment is when you retry, not whether."""

    def test_the_salary_window_is_what_changes_the_answer(self) -> None:
        spec = SEGMENTS_BY_ID[SegmentId.INSUFFICIENT_FUNDS_SALARY_CYCLE]
        inside = spec.resolve(covariates(salary_window=True))
        outside = spec.resolve(covariates(salary_window=False))

        assert inside.uplift_bps(Action.RETRY_PAYMENT) > outside.uplift_bps(Action.RETRY_PAYMENT)
        assert inside.y1_bps[Action.RETRY_PAYMENT] == 5_500

    def test_the_baseline_is_the_same_either_way(self) -> None:
        """The account is empty regardless; only the response differs."""
        spec = SEGMENTS_BY_ID[SegmentId.INSUFFICIENT_FUNDS_SALARY_CYCLE]
        assert spec.resolve(covariates(salary_window=True)).y0_bps == (
            spec.resolve(covariates(salary_window=False)).y0_bps
        )


class TestSegment3ActionSpecific:
    """The segment that makes a scalar y1 untenable."""

    def test_a_payment_link_works_and_a_retry_does_not(self) -> None:
        probabilities = SEGMENTS_BY_ID[SegmentId.EXPIRED_OR_BLOCKED_CARD].resolve(covariates())
        assert probabilities.y1_bps[Action.CREATE_PAYMENT_LINK] == 6_000
        assert probabilities.y1_bps[Action.RETRY_PAYMENT] == 600

    def test_the_choice_of_action_matters_more_than_acting(self) -> None:
        probabilities = SEGMENTS_BY_ID[SegmentId.EXPIRED_OR_BLOCKED_CARD].resolve(covariates())
        link = probabilities.uplift_bps(Action.CREATE_PAYMENT_LINK)
        retry = probabilities.uplift_bps(Action.RETRY_PAYMENT)
        assert link > 10 * retry

    def test_best_action_picks_the_link(self) -> None:
        probabilities = SEGMENTS_BY_ID[SegmentId.EXPIRED_OR_BLOCKED_CARD].resolve(covariates())
        assert probabilities.best_action() is Action.CREATE_PAYMENT_LINK


class TestSegment4Timing:
    def test_recovery_arrives_with_the_clock(self) -> None:
        spec = SEGMENTS_BY_ID[SegmentId.ISSUER_DOWNTIME]
        after = spec.resolve(covariates(downtime_ended=True))
        during = spec.resolve(covariates(downtime_ended=False))

        assert after.y0_bps == 7_000
        assert during.y0_bps == 1_000

    def test_the_nudge_adds_nothing_once_the_issuer_returns(self) -> None:
        spec = SEGMENTS_BY_ID[SegmentId.ISSUER_DOWNTIME]
        after = spec.resolve(covariates(downtime_ended=True))
        assert abs(after.uplift_bps(Action.RETRY_PAYMENT)) <= 100


class TestSegment5LostCause:
    def test_contact_buys_almost_nothing(self) -> None:
        probabilities = SEGMENTS_BY_ID[SegmentId.INTENTIONAL_CHURNER].resolve(covariates())
        assert probabilities.y0_bps == 200
        for action in CANDIDATE_ACTIONS:
            assert probabilities.uplift_bps(action) < 300


class TestSegment6SleepingDog:
    """The headline. Planted so the engine can find it unaided."""

    def test_recovery_barely_moves(self) -> None:
        probabilities = SEGMENTS_BY_ID[SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER].resolve(
            covariates()
        )
        for action in CANDIDATE_ACTIONS:
            assert probabilities.uplift_bps(action) <= 100

    def test_contacting_raises_harm_by_at_least_eight_points(self) -> None:
        probabilities = SEGMENTS_BY_ID[SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER].resolve(
            covariates()
        )
        for action in CANDIDATE_ACTIONS:
            assert probabilities.harm_uplift_bps(action) >= 800, action

    def test_harm_uplift_dwarfs_recovery_uplift(self) -> None:
        """This is what makes it a sleeping dog rather than a weak win."""
        probabilities = SEGMENTS_BY_ID[SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER].resolve(
            covariates()
        )
        for action in CANDIDATE_ACTIONS:
            assert probabilities.harm_uplift_bps(action) > probabilities.uplift_bps(action)

    def test_it_is_the_only_planted_sleeping_dog(self) -> None:
        from app.models.enums import Quadrant

        dogs = [s.id for s in SEGMENTS if s.expected_quadrant is Quadrant.SLEEPING_DOG]
        assert dogs == [SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER]

    def test_it_would_pay_anyway(self) -> None:
        """High y0 is why acting is pure downside."""
        probabilities = SEGMENTS_BY_ID[SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER].resolve(
            covariates()
        )
        assert probabilities.y0_bps >= 6_000


class TestSegment7HighValue:
    def test_the_uplift_is_genuinely_large(self) -> None:
        probabilities = SEGMENTS_BY_ID[SegmentId.HIGH_VALUE_CUSTOMER].resolve(covariates())
        assert probabilities.y0_bps == 3_000
        assert probabilities.uplift_bps(Action.CREATE_PAYMENT_LINK) >= 2_000


class TestExpectedQuadrants:
    def test_every_quadrant_worth_planting_is_represented(self) -> None:
        from app.models.enums import Quadrant

        planted = {spec.expected_quadrant for spec in SEGMENTS}
        assert planted == {
            Quadrant.SURE_THING,
            Quadrant.PERSUADABLE,
            Quadrant.LOST_CAUSE,
            Quadrant.SLEEPING_DOG,
        }

    def test_gray_zone_is_not_planted(self) -> None:
        """Gray zone is what a model says when it does not know. Planting it
        would confuse a real segment with an absence of evidence."""
        from app.models.enums import Quadrant

        assert Quadrant.GRAY_ZONE not in {spec.expected_quadrant for spec in SEGMENTS}
