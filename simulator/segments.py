"""The seven planted behavioural segments.

Every parameter here is an **assumption written by hand**, not a measurement.
Nothing in this file was fitted to real Razorpay traffic, and the evaluation
report must say so wherever these numbers appear. What the segments buy is a
known answer: because the ground truth was planted, the estimator can be scored
against it, and an estimator that cannot recover a planted effect is broken
regardless of how confident its output looks.

Two segments carry the weight of the whole project:

* **Segment 1 (transient UPI timeout)** has `y0 = 0.75`. Three quarters of these
  customers pay on their own. Any product reporting gross recovery credits
  itself for all of them, which is exactly the inflation the ledger exists to
  expose.
* **Segment 6 (low-engagement mandate holder)** has a *negative* effect on a
  harm metric: contacting them raises mandate cancellation by 8 percentage
  points while barely moving recovery. It is planted so the engine can
  rediscover it without being told it exists.

**Probabilities are integer basis points, never floats.** `7500` is 0.75. The
simulator's existing rule is that no parameter may be float-valued, so that
arithmetic stays exactly reproducible, and the Day 1 schema stores the same way.
A Bernoulli draw is `rng.randint(1, 10000) <= p_bps`.

**`y1` is per action.** Segment 3 is the reason: an expired card responds to a
payment-link that lets the customer update their instrument (0.60) and barely at
all to a blind retry (0.06). A model with one scalar `y1` could not represent
that, and the abstention gate would have nothing to reason about.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.models.enums import Quadrant

#: Full scale for a basis-point probability.
BPS_SCALE = 10_000


class SegmentId(StrEnum):
    """The seven planted populations."""

    TRANSIENT_UPI_TIMEOUT = "transient_upi_timeout"
    INSUFFICIENT_FUNDS_SALARY_CYCLE = "insufficient_funds_salary_cycle"
    EXPIRED_OR_BLOCKED_CARD = "expired_or_blocked_card"
    ISSUER_DOWNTIME = "issuer_downtime"
    INTENTIONAL_CHURNER = "intentional_churner"
    LOW_ENGAGEMENT_MANDATE_HOLDER = "low_engagement_mandate_holder"
    HIGH_VALUE_CUSTOMER = "high_value_customer"


class Action(StrEnum):
    """The bounded action set, matching the Day 1 intervention catalogue."""

    NO_ACTION = "no_action"
    RETRY_PAYMENT = "retry_payment"
    CREATE_PAYMENT_LINK = "create_payment_link"


#: Actions an experiment may actually take. `NO_ACTION` is what a holdout gets
#: and is never a candidate intervention.
CANDIDATE_ACTIONS: tuple[Action, ...] = (Action.RETRY_PAYMENT, Action.CREATE_PAYMENT_LINK)


@dataclass(frozen=True, slots=True)
class Covariates:
    """Per-case features a segment's response may depend on.

    These are observable — they are what the Day 5 uplift model gets to learn
    from. The segment label itself is *not* observable, which is the point: the
    model must rediscover the heterogeneity from these, not be handed it.
    """

    #: True when the retry would land on or after a salary date. Segment 2.
    salary_window: bool
    #: True when the issuer's downtime window has ended. Segment 4.
    downtime_ended: bool
    day_of_month: int
    amount_minor: int
    payment_method: str
    issuer: str
    #: Days since the customer first transacted.
    tenure_days: int
    prior_recovery_count: int
    prior_contact_count_30d: int
    hour_of_day: int


@dataclass(frozen=True, slots=True)
class OutcomeProbabilities:
    """Both potential outcomes for one case, before any coin is flipped.

    `y0_bps` is what would happen with no intervention — the self-recovery
    probability the industry quietly credits to itself.
    """

    y0_bps: int
    y1_bps: Mapping[Action, int]
    harm0_bps: int
    harm1_bps: Mapping[Action, int]

    def __post_init__(self) -> None:
        for label, value in (("y0_bps", self.y0_bps), ("harm0_bps", self.harm0_bps)):
            _require_bps(value, label)
        for action, value in self.y1_bps.items():
            _require_bps(value, f"y1_bps[{action}]")
        for action, value in self.harm1_bps.items():
            _require_bps(value, f"harm1_bps[{action}]")

        missing = set(CANDIDATE_ACTIONS) - set(self.y1_bps)
        if missing:
            raise ValueError(f"y1_bps must cover every candidate action; missing {sorted(missing)}")

    def uplift_bps(self, action: Action) -> int:
        """Ground-truth treatment effect on recovery. May be negative."""
        return self.y1_bps[action] - self.y0_bps

    def harm_uplift_bps(self, action: Action) -> int:
        """Ground-truth treatment effect on harm. Positive means acting hurts."""
        return self.harm1_bps[action] - self.harm0_bps

    def best_action(self) -> Action:
        """The action with the highest recovery uplift, ties broken by name."""
        return min(CANDIDATE_ACTIONS, key=lambda a: (-self.uplift_bps(a), a.value))


def _require_bps(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer in basis points, got {type(value).__name__}")
    if not 0 <= value <= BPS_SCALE:
        raise ValueError(f"{label} must be within 0..{BPS_SCALE}, got {value}")


@dataclass(frozen=True, slots=True)
class SegmentSpec:
    """One planted population, with its documented rationale."""

    id: SegmentId
    label: str
    #: Share of generated cases, in basis points. The mix sums to 10000.
    weight_bps: int
    #: Where a correct uplift model should place this segment on Day 5. Ground
    #: truth for the quadrant confusion matrix, never an input to scoring.
    expected_quadrant: Quadrant
    #: Why this population exists in the plan.
    rationale: str
    resolve: Callable[[Covariates], OutcomeProbabilities]
    #: Failure code these cases present with, so the segment is not trivially
    #: identifiable from a single observable.
    failure_code: str
    preferred_method: str


# -- segment 1: transient UPI timeout -------------------------------------


def _transient_upi_timeout(covariates: Covariates) -> OutcomeProbabilities:
    """Self-recovery is the norm. This is where fake recovery comes from.

    Three quarters pay unprompted, usually within minutes, because a UPI
    timeout is a transport failure rather than an inability or unwillingness to
    pay. Intervening adds almost nothing, and every rupee of the 0.75 is credited
    as "recovered" by a product that does not run a holdout.
    """
    return OutcomeProbabilities(
        y0_bps=7_500,
        y1_bps={Action.RETRY_PAYMENT: 7_800, Action.CREATE_PAYMENT_LINK: 7_700},
        harm0_bps=50,
        harm1_bps={Action.RETRY_PAYMENT: 60, Action.CREATE_PAYMENT_LINK: 80},
    )


# -- segment 2: insufficient funds, salary cycle --------------------------


def _insufficient_funds_salary_cycle(covariates: Covariates) -> OutcomeProbabilities:
    """Timing is the whole treatment.

    The money genuinely is not in the account. Retrying immediately fails again;
    retrying once salary has landed succeeds far more often. The heterogeneity
    is conditional on an *observable* — `salary_window` — so the Day 5 model can
    learn it rather than being handed the segment label.
    """
    if covariates.salary_window:
        return OutcomeProbabilities(
            y0_bps=1_500,
            y1_bps={Action.RETRY_PAYMENT: 5_500, Action.CREATE_PAYMENT_LINK: 4_800},
            harm0_bps=40,
            harm1_bps={Action.RETRY_PAYMENT: 60, Action.CREATE_PAYMENT_LINK: 90},
        )
    # Outside the salary window the account is still empty, so acting is close
    # to worthless — the same customer, a different moment.
    return OutcomeProbabilities(
        y0_bps=1_500,
        y1_bps={Action.RETRY_PAYMENT: 2_000, Action.CREATE_PAYMENT_LINK: 2_200},
        harm0_bps=40,
        harm1_bps={Action.RETRY_PAYMENT: 80, Action.CREATE_PAYMENT_LINK: 120},
    )


# -- segment 3: expired or blocked card -----------------------------------


def _expired_or_blocked_card(covariates: Covariates) -> OutcomeProbabilities:
    """The action matters more than whether you act at all.

    The instrument is dead. A blind retry hits the same dead card and gains
    almost nothing (0.05 → 0.06). A payment link that lets the customer enter a
    new instrument works (0.05 → 0.60). This is the segment that makes a scalar
    `y1` untenable and gives the recovery engine something real to choose
    between.
    """
    return OutcomeProbabilities(
        y0_bps=500,
        y1_bps={Action.RETRY_PAYMENT: 600, Action.CREATE_PAYMENT_LINK: 6_000},
        harm0_bps=30,
        harm1_bps={Action.RETRY_PAYMENT: 50, Action.CREATE_PAYMENT_LINK: 70},
    )


# -- segment 4: issuer downtime -------------------------------------------


def _issuer_downtime(covariates: Covariates) -> OutcomeProbabilities:
    """Recovery arrives with the clock, not with the nudge.

    While the issuer is down nothing works, for treated and untreated alike.
    Once the window passes, payment succeeds either way. A product that
    intervenes during recovery and measures afterwards will credit itself for
    the issuer coming back up.
    """
    if covariates.downtime_ended:
        return OutcomeProbabilities(
            y0_bps=7_000,
            y1_bps={Action.RETRY_PAYMENT: 7_000, Action.CREATE_PAYMENT_LINK: 6_900},
            harm0_bps=60,
            harm1_bps={Action.RETRY_PAYMENT: 90, Action.CREATE_PAYMENT_LINK: 110},
        )
    return OutcomeProbabilities(
        y0_bps=1_000,
        y1_bps={Action.RETRY_PAYMENT: 1_100, Action.CREATE_PAYMENT_LINK: 1_300},
        harm0_bps=60,
        harm1_bps={Action.RETRY_PAYMENT: 100, Action.CREATE_PAYMENT_LINK: 130},
    )


# -- segment 5: intentional churner ---------------------------------------


def _intentional_churner(covariates: Covariates) -> OutcomeProbabilities:
    """They have decided to leave. Contacting them changes almost nothing.

    Not harmful, just wasted: every contact costs money and buys 2 percentage
    points. A system that acts on everything spends its budget here.
    """
    return OutcomeProbabilities(
        y0_bps=200,
        y1_bps={Action.RETRY_PAYMENT: 400, Action.CREATE_PAYMENT_LINK: 450},
        harm0_bps=100,
        harm1_bps={Action.RETRY_PAYMENT: 140, Action.CREATE_PAYMENT_LINK: 180},
    )


# -- segment 6: low-engagement mandate holder -----------------------------


def _low_engagement_mandate_holder(covariates: Covariates) -> OutcomeProbabilities:
    """**The sleeping dog.** Contacting them destroys value.

    They pay reliably (0.65) as long as nobody reminds them the mandate exists.
    A recovery nudge moves recovery by 1 percentage point and raises mandate
    cancellation by **8 percentage points** — trading a small one-off gain for a
    large recurring loss.

    This is the population planted so the engine can find it unaided. Every
    number in this project is arranged so that finding it is possible and
    missing it is visible.
    """
    return OutcomeProbabilities(
        y0_bps=6_500,
        y1_bps={Action.RETRY_PAYMENT: 6_600, Action.CREATE_PAYMENT_LINK: 6_550},
        harm0_bps=200,
        # +8 percentage points of mandate cancellation, either way it is
        # contacted. The payment link is marginally worse: it is the louder
        # touch.
        harm1_bps={Action.RETRY_PAYMENT: 1_000, Action.CREATE_PAYMENT_LINK: 1_100},
    )


# -- segment 7: high-value customer ---------------------------------------


def _high_value_customer(covariates: Covariates) -> OutcomeProbabilities:
    """Genuinely persuadable, and each one is worth more.

    The uplift is large (0.30 → 0.55) and the amounts are high, so this segment
    dominates net incremental value even though it is the smallest population.
    Prioritisation by expected value, not by uplift alone, is what surfaces it.
    """
    return OutcomeProbabilities(
        y0_bps=3_000,
        y1_bps={Action.RETRY_PAYMENT: 5_000, Action.CREATE_PAYMENT_LINK: 5_500},
        harm0_bps=80,
        harm1_bps={Action.RETRY_PAYMENT: 150, Action.CREATE_PAYMENT_LINK: 200},
    )


#: The planted mix. Weights sum to 10000 bps and are chosen so every segment
#: has enough mass to be estimable at the benchmark size — segment 6 in
#: particular, because an undetectable sleeping dog would prove nothing.
SEGMENTS: tuple[SegmentSpec, ...] = (
    SegmentSpec(
        id=SegmentId.TRANSIENT_UPI_TIMEOUT,
        label="Transient UPI timeout",
        weight_bps=2_500,
        expected_quadrant=Quadrant.SURE_THING,
        rationale="Self-recovers without help; the source of industry-wide gross inflation.",
        resolve=_transient_upi_timeout,
        failure_code="gateway_timeout",
        preferred_method="upi",
    ),
    SegmentSpec(
        id=SegmentId.INSUFFICIENT_FUNDS_SALARY_CYCLE,
        label="Insufficient funds, salary cycle",
        weight_bps=2_000,
        expected_quadrant=Quadrant.PERSUADABLE,
        rationale="Responds strongly, but only when retried after salary lands.",
        resolve=_insufficient_funds_salary_cycle,
        failure_code="insufficient_funds",
        preferred_method="card",
    ),
    SegmentSpec(
        id=SegmentId.EXPIRED_OR_BLOCKED_CARD,
        label="Expired or blocked card",
        weight_bps=1_500,
        expected_quadrant=Quadrant.PERSUADABLE,
        rationale="Responds to a payment link only; a retry hits the same dead instrument.",
        resolve=_expired_or_blocked_card,
        failure_code="card_declined",
        preferred_method="card",
    ),
    SegmentSpec(
        id=SegmentId.ISSUER_DOWNTIME,
        label="Issuer downtime window",
        weight_bps=1_000,
        expected_quadrant=Quadrant.SURE_THING,
        rationale="Recovers when the issuer returns; the nudge takes credit for the clock.",
        resolve=_issuer_downtime,
        failure_code="bank_unavailable",
        preferred_method="netbanking",
    ),
    SegmentSpec(
        id=SegmentId.INTENTIONAL_CHURNER,
        label="Intentional churner",
        weight_bps=1_000,
        expected_quadrant=Quadrant.LOST_CAUSE,
        rationale="Has decided to leave; contact buys almost nothing and costs money.",
        resolve=_intentional_churner,
        failure_code="card_declined",
        preferred_method="card",
    ),
    SegmentSpec(
        id=SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER,
        label="Low-engagement mandate holder",
        weight_bps=1_500,
        expected_quadrant=Quadrant.SLEEPING_DOG,
        rationale="Contacting raises mandate cancellation by 8pp for ~1pp of recovery.",
        resolve=_low_engagement_mandate_holder,
        failure_code="mandate_inactive",
        preferred_method="upi",
    ),
    SegmentSpec(
        id=SegmentId.HIGH_VALUE_CUSTOMER,
        label="High-value customer",
        weight_bps=500,
        expected_quadrant=Quadrant.PERSUADABLE,
        rationale="Large genuine uplift on large amounts; dominates net value.",
        resolve=_high_value_customer,
        failure_code="insufficient_funds",
        preferred_method="card",
    ),
)

SEGMENTS_BY_ID: Mapping[SegmentId, SegmentSpec] = {spec.id: spec for spec in SEGMENTS}


def total_weight_bps() -> int:
    return sum(spec.weight_bps for spec in SEGMENTS)


def segment_for_draw(draw_bps: int) -> SegmentSpec:
    """Pick a segment from a draw in 1..10000, by cumulative weight.

    Deterministic and integer-only: the same draw always yields the same
    segment, in a fixed declaration order.
    """
    if not 1 <= draw_bps <= BPS_SCALE:
        raise ValueError(f"draw_bps must be within 1..{BPS_SCALE}, got {draw_bps}")

    cumulative = 0
    for spec in SEGMENTS:
        cumulative += spec.weight_bps
        if draw_bps <= cumulative:
            return spec
    return SEGMENTS[-1]  # pragma: no cover - guarded by total_weight_bps()


__all__ = [
    "BPS_SCALE",
    "CANDIDATE_ACTIONS",
    "SEGMENTS",
    "SEGMENTS_BY_ID",
    "Action",
    "Covariates",
    "OutcomeProbabilities",
    "SegmentId",
    "SegmentSpec",
    "segment_for_draw",
    "total_weight_bps",
]
