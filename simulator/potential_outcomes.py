"""Potential-outcomes generator — the rebuild that makes the ledger honest.

For every case this draws **both** outcomes: what the customer would do if left
alone (`y0`), and what they would do under each candidate action (`y1[action]`).
No real system can observe both — that is the fundamental problem of causal
inference — and the whole point of simulating is that here we can, so the
estimator has a known answer to be scored against.

**The reveal is the load-bearing part.** `PotentialOutcomeCase` holds the truth
and never leaves the simulator. `RevealedCase` holds only the outcome matching
the assigned arm, and is what the application pipeline receives. They are
separate types rather than one type with a flag, because a flag can be read the
wrong way and a missing field cannot.

**Draws are assignment-independent.** `y0` and every `y1` are drawn from
sub-streams derived from the case identity alone, never from the arm. If the arm
influenced the draw, the "potential" outcomes would not be potential — they
would be consequences of the assignment, and the ground-truth ATE would be an
artefact of the randomiser rather than of the segments.

Determinism throughout: integer basis points, seeded sub-streams, no floats, no
clock, no `uuid.uuid4()`. Re-running with the same seed reproduces every case
byte for byte, verified by checksum.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from simulator.clock import SIMULATION_EPOCH, SimulationClock
from simulator.rng import DeterministicRng
from simulator.segments import (
    BPS_SCALE,
    CANDIDATE_ACTIONS,
    SEGMENTS_BY_ID,
    Action,
    Covariates,
    OutcomeProbabilities,
    SegmentId,
    segment_for_draw,
    total_weight_bps,
)

#: Version of the potential-outcomes generator. Separate from the v1
#: GENERATOR_VERSION on purpose: bumping that would move every existing scenario
#: checksum and break 1,587 passing tests for no reason.
POTENTIAL_OUTCOMES_VERSION = "2.0.0"

#: Benchmark size. The revised plan calls for 8,000-12,000 cases.
DEFAULT_CASE_COUNT = 10_000

#: Amount bands in minor units. The 15,00,000 paise boundary is the RBI
#: additional-factor-authentication threshold for recurring debits, so cases
#: either side of it face different regulatory constraints.
AMOUNT_BANDS: tuple[tuple[str, int, int], ...] = (
    ("<500", 10_000, 49_999),
    ("500-2000", 50_000, 199_999),
    ("2000-5000", 200_000, 499_999),
    ("5000-15000", 500_000, 1_499_999),
    (">15000", 1_500_000, 5_000_000),
)

#: Weight per band, in basis points. Small tickets dominate, as they do in life.
AMOUNT_BAND_WEIGHTS_BPS: tuple[int, ...] = (3_000, 3_500, 2_000, 1_200, 300)

ISSUERS: tuple[str, ...] = ("hdfc", "icici", "sbi", "axis", "kotak")

#: Months are treated as a uniform 30 days. A calendar would add nothing here
#: and would make the salary-window share vary by month for no modelled reason.
DAYS_IN_SIMULATED_MONTH = 30

#: Days on which salary typically lands, so a retry then has money to find.
#: Segment 2 keys off this. Eleven of thirty days, so roughly a third of that
#: segment sits in the responsive window.
SALARY_WINDOW_DAYS: frozenset[int] = frozenset({25, 26, 27, 28, 29, 30, 1, 2, 3, 4, 5})


def _bernoulli(rng: DeterministicRng, probability_bps: int) -> bool:
    """A coin flip with integer-only arithmetic.

    `randint(1, 10000) <= p` rather than a float comparison, so the draw is
    exactly reproducible and a probability of 0 or 10000 behaves correctly at
    the boundary.
    """
    if probability_bps <= 0:
        return False
    if probability_bps >= BPS_SCALE:
        return True
    return rng.randint(1, BPS_SCALE) <= probability_bps


def _weighted_index(rng: DeterministicRng, weights_bps: Sequence[int]) -> int:
    draw = rng.randint(1, sum(weights_bps))
    cumulative = 0
    for index, weight in enumerate(weights_bps):
        cumulative += weight
        if draw <= cumulative:
            return index
    return len(weights_bps) - 1  # pragma: no cover - unreachable while weights sum


@dataclass(frozen=True, slots=True)
class PotentialOutcomeCase:
    """One case with both potential outcomes.

    **Simulator-only.** Nothing in `app/` may consume this type. The realised
    outcomes `y0` and `y1` are the answer key; `reveal()` produces the view the
    application is allowed to see.
    """

    case_id: uuid.UUID
    segment_id: SegmentId
    covariates: Covariates
    probabilities: OutcomeProbabilities

    #: Realised potential outcomes. Both are drawn; at most one is ever observed.
    y0: bool
    y1: Mapping[Action, bool]
    harm0: bool
    harm1: Mapping[Action, bool]

    #: Minor units that would arrive if the case recovers.
    amount_minor: int
    detected_at: datetime

    def outcome_for(self, action: Action) -> bool:
        """Realised recovery under one action. `NO_ACTION` is the control."""
        return self.y0 if action is Action.NO_ACTION else self.y1[action]

    def harm_for(self, action: Action) -> bool:
        return self.harm0 if action is Action.NO_ACTION else self.harm1[action]

    def true_uplift_bps(self, action: Action) -> int:
        """Ground-truth effect for this case's segment. Never an estimator input."""
        return self.probabilities.uplift_bps(action)

    def true_harm_uplift_bps(self, action: Action) -> int:
        return self.probabilities.harm_uplift_bps(action)

    def reveal(self, action: Action) -> RevealedCase:
        """Collapse to the single outcome the assigned arm actually produces.

        Everything else — the other arm's outcome, the segment label, the true
        probabilities — is dropped here and cannot travel further.
        """
        return RevealedCase(
            case_id=self.case_id,
            covariates=self.covariates,
            action=action,
            recovered=self.outcome_for(action),
            harmed=self.harm_for(action),
            amount_minor=self.amount_minor if self.outcome_for(action) else 0,
            detected_at=self.detected_at,
        )


@dataclass(frozen=True, slots=True)
class RevealedCase:
    """What the application is allowed to see.

    Deliberately carries no `y0`, no `y1`, no segment label, and no true
    probabilities. A pipeline handed one of these cannot read the answer even by
    accident, because the fields simply are not there.
    """

    case_id: uuid.UUID
    covariates: Covariates
    action: Action
    recovered: bool
    harmed: bool
    amount_minor: int
    detected_at: datetime


@dataclass(frozen=True, slots=True)
class PotentialOutcomeSet:
    """A generated benchmark population plus its reproduction metadata."""

    cases: tuple[PotentialOutcomeCase, ...]
    seed: int
    generator_version: str = POTENTIAL_OUTCOMES_VERSION

    def by_segment(self, segment_id: SegmentId) -> tuple[PotentialOutcomeCase, ...]:
        return tuple(case for case in self.cases if case.segment_id is segment_id)

    def __len__(self) -> int:
        return len(self.cases)


def _covariates(rng: DeterministicRng, segment_id: SegmentId) -> tuple[Covariates, int]:
    """Draw the observable features, plus the amount in minor units.

    Segment membership nudges some observables — a UPI timeout tends to arrive
    on UPI — but never determines them, so the segment stays genuinely latent
    rather than readable off a single column.
    """
    spec = SEGMENTS_BY_ID[segment_id]

    band_index = _weighted_index(rng, AMOUNT_BAND_WEIGHTS_BPS)
    _, low, high = AMOUNT_BANDS[band_index]
    amount_minor = rng.randint(low, high)

    # High-value customers skew to the top bands, which is what makes them
    # dominate net value rather than uplift alone.
    if segment_id is SegmentId.HIGH_VALUE_CUSTOMER:
        amount_minor = rng.randint(1_000_000, 5_000_000)

    # 70% of the time the segment's characteristic method shows up; the rest is
    # noise, so method alone never identifies the segment.
    method = (
        spec.preferred_method
        if rng.randint(1, 100) <= 70
        else rng.choice(("card", "upi", "netbanking"))
    )

    # Uniform 30-day month: a deliberate simplification, and one that matters
    # here because drawing 1..28 would have silently truncated half the salary
    # window and weakened the very mechanism segment 2 plants.
    day_of_month = rng.randint(1, DAYS_IN_SIMULATED_MONTH)
    return (
        Covariates(
            salary_window=day_of_month in SALARY_WINDOW_DAYS,
            downtime_ended=rng.randint(1, 100) <= 60,
            day_of_month=day_of_month,
            amount_minor=amount_minor,
            payment_method=method,
            issuer=rng.choice(ISSUERS),
            tenure_days=rng.randint(1, 1_460),
            prior_recovery_count=rng.randint(0, 3),
            prior_contact_count_30d=rng.randint(0, 2),
            hour_of_day=rng.randint(0, 23),
        ),
        amount_minor,
    )


def generate_case(
    rng: DeterministicRng,
    case_index: int,
    clock: SimulationClock,
) -> PotentialOutcomeCase:
    """Generate one case, drawing every potential outcome.

    Each concern gets its own derived sub-stream, so adding a draw to one does
    not silently shift every value in the others — the same discipline the v1
    simulator uses, for the same reason.
    """
    case_rng = rng.derive(f"case.{case_index}")

    segment_rng = case_rng.derive("segment")
    spec = segment_for_draw(segment_rng.randint(1, total_weight_bps()))

    covariate_rng = case_rng.derive("covariates")
    covariates, amount_minor = _covariates(covariate_rng, spec.id)

    probabilities = spec.resolve(covariates)

    # Independent sub-streams per potential outcome. Crucially none of these is
    # derived from an arm: the draws exist before any assignment happens, which
    # is what makes them *potential* outcomes.
    y0 = _bernoulli(case_rng.derive("y0"), probabilities.y0_bps)
    y1 = {
        action: _bernoulli(case_rng.derive(f"y1.{action.value}"), probabilities.y1_bps[action])
        for action in CANDIDATE_ACTIONS
    }
    harm0 = _bernoulli(case_rng.derive("harm0"), probabilities.harm0_bps)
    harm1 = {
        action: _bernoulli(
            case_rng.derive(f"harm1.{action.value}"), probabilities.harm1_bps[action]
        )
        for action in CANDIDATE_ACTIONS
    }

    return PotentialOutcomeCase(
        case_id=case_rng.derive("identity").uuid(),
        segment_id=spec.id,
        covariates=covariates,
        probabilities=probabilities,
        y0=y0,
        y1=y1,
        harm0=harm0,
        harm1=harm1,
        amount_minor=amount_minor,
        detected_at=clock.at(case_index * 60),
    )


def generate(
    *,
    seed: int,
    case_count: int = DEFAULT_CASE_COUNT,
    epoch: datetime = SIMULATION_EPOCH,
) -> PotentialOutcomeSet:
    """Generate a benchmark population.

    Pure: no I/O, no database, no network, no clock. The same seed and count
    always produce identical cases.
    """
    if isinstance(case_count, bool) or not isinstance(case_count, int):
        raise TypeError(f"case_count must be an int, got {type(case_count).__name__}")
    if case_count < 1:
        raise ValueError(f"case_count must be at least 1, got {case_count}")

    root = DeterministicRng(seed, label="potential_outcomes")
    clock = SimulationClock(epoch)

    return PotentialOutcomeSet(
        cases=tuple(generate_case(root, index, clock) for index in range(case_count)),
        seed=seed,
    )


# -- ground truth ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SegmentTruth:
    """Ground-truth effects for one segment, computed from realised outcomes.

    **Only the evaluation report may read this.** It is the answer key: an
    estimator that consumed it would be scoring itself against its own input.
    """

    segment_id: SegmentId
    n: int
    action: Action
    #: Realised rates, in basis points.
    y0_rate_bps: int
    y1_rate_bps: int
    #: The true average treatment effect for this segment. May be negative.
    true_ate_bps: int
    harm0_rate_bps: int
    harm1_rate_bps: int
    true_harm_ate_bps: int
    expected_quadrant: str

    @property
    def is_sleeping_dog(self) -> bool:
        """Acting raises harm materially without buying meaningful recovery."""
        return self.true_harm_ate_bps > 0 and self.true_ate_bps <= self.true_harm_ate_bps


def _rate_bps(count: int, total: int) -> int:
    """Integer basis points, half-up. No float enters the calculation."""
    if total == 0:
        return 0
    return (count * BPS_SCALE + total // 2) // total


def segment_truth(
    population: PotentialOutcomeSet,
    segment_id: SegmentId,
    action: Action,
) -> SegmentTruth:
    """Ground-truth ATE for one segment under one action.

    Computed from the **realised** potential outcomes rather than from the
    planted probabilities, so it reflects sampling variation exactly as the
    estimator will see it.
    """
    cases = population.by_segment(segment_id)
    n = len(cases)

    y0_hits = sum(1 for case in cases if case.y0)
    y1_hits = sum(1 for case in cases if case.y1[action])
    harm0_hits = sum(1 for case in cases if case.harm0)
    harm1_hits = sum(1 for case in cases if case.harm1[action])

    y0_rate = _rate_bps(y0_hits, n)
    y1_rate = _rate_bps(y1_hits, n)
    harm0_rate = _rate_bps(harm0_hits, n)
    harm1_rate = _rate_bps(harm1_hits, n)

    return SegmentTruth(
        segment_id=segment_id,
        n=n,
        action=action,
        y0_rate_bps=y0_rate,
        y1_rate_bps=y1_rate,
        true_ate_bps=y1_rate - y0_rate,
        harm0_rate_bps=harm0_rate,
        harm1_rate_bps=harm1_rate,
        true_harm_ate_bps=harm1_rate - harm0_rate,
        expected_quadrant=SEGMENTS_BY_ID[segment_id].expected_quadrant.value,
    )


def truth_by_segment(
    population: PotentialOutcomeSet,
    action: Action = Action.CREATE_PAYMENT_LINK,
) -> tuple[SegmentTruth, ...]:
    """Ground-truth ATE for every segment, in declaration order."""
    return tuple(segment_truth(population, spec_id, action) for spec_id in SEGMENTS_BY_ID)


def overall_truth(
    population: PotentialOutcomeSet,
    action: Action = Action.CREATE_PAYMENT_LINK,
) -> tuple[int, int]:
    """Population-wide true ATE and true harm ATE, in basis points."""
    cases = population.cases
    n = len(cases)

    ate = _rate_bps(sum(1 for c in cases if c.y1[action]), n) - _rate_bps(
        sum(1 for c in cases if c.y0), n
    )
    harm_ate = _rate_bps(sum(1 for c in cases if c.harm1[action]), n) - _rate_bps(
        sum(1 for c in cases if c.harm0), n
    )
    return ate, harm_ate


def self_recovery_share_bps(population: PotentialOutcomeSet) -> int:
    """Share of the population that would have paid with no intervention.

    The single most important number in the pitch: money a gross-recovery
    dashboard credits itself for without having caused any of it.
    """
    return _rate_bps(sum(1 for case in population.cases if case.y0), len(population.cases))


#: Public alias. `generate` alone would sit ambiguously next to `simulate` in
#: the package namespace; inside this module the short name reads better.
generate_potential_outcomes = generate

__all__ = [
    "AMOUNT_BANDS",
    "DEFAULT_CASE_COUNT",
    "POTENTIAL_OUTCOMES_VERSION",
    "PotentialOutcomeCase",
    "PotentialOutcomeSet",
    "RevealedCase",
    "SegmentTruth",
    "generate",
    "generate_case",
    "generate_potential_outcomes",
    "overall_truth",
    "segment_truth",
    "self_recovery_share_bps",
    "truth_by_segment",
]


def observation_window_end(case: PotentialOutcomeCase, hours: int) -> datetime:
    """Window close for one case. Supplied hours, never a default clock."""
    return case.detected_at + timedelta(hours=hours)
