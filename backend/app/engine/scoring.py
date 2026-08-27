"""Deterministic confidence scoring.

**`confidence_bps` is a synthetic/demo heuristic, not a validated probability.**

It does not estimate how likely a risk is to be real, because nothing in this
project has been calibrated against outcomes that would justify such a claim. It
is a transparent, reproducible measure of *how much evidence supports the
finding* — more corroborating failures raise it, missing evidence lowers it.

Anywhere it is displayed it must be labelled as such. Treating it as a
probability would be a false claim of rigour, and CLAUDE.md is explicit that an
arbitrary score must not be dressed up as a scientific prediction.

Why integers: confidence is stored and compared in basis points (0-10000)
because policy thresholds in Phase 7 will gate money on it. Float comparison is
not a safe gate — two runs that should agree must agree exactly.

Every component below is an integer constant. The arithmetic is addition and
clamping, nothing else, and `explain()` returns the exact terms so a reviewer
can check the sum by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.tracing.state import OrderTimeline, SubscriptionTimeline

MIN_CONFIDENCE_BPS = 0
MAX_CONFIDENCE_BPS = 10_000

#: Starting evidence weight per risk type.
BASE_REPEATED_FAILURE_BPS = 6_000
BASE_CHECKOUT_ABANDONMENT_BPS = 5_500
BASE_SUBSCRIPTION_FAILURE_BPS = 6_000
BASE_RECONCILIATION_BPS = 7_000

#: Each failure beyond the two required to fire corroborates the finding.
PER_EXTRA_FAILURE_BPS = 1_000
MAX_EXTRA_FAILURE_BONUS_BPS = 3_000

#: An explicit abandonment event is stronger evidence than inferred silence.
EXPLICIT_ABANDONMENT_BONUS_BPS = 2_000

#: A halted subscription confirms the provider gave up too.
SUBSCRIPTION_HALTED_BONUS_BPS = 1_500

#: Evidence we know is missing lowers confidence without suppressing the finding.
PER_INFERRED_GAP_PENALTY_BPS = 750
MAX_INFERRED_GAP_PENALTY_BPS = 3_000

#: A high-value order is not stronger evidence, so amount deliberately does not
#: appear in this score. Prioritisation is the amount's job, not confidence's.


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    """The arithmetic behind a score, so it can be checked by hand."""

    risk_type: str
    total_bps: int
    components: tuple[tuple[str, int], ...] = ()
    #: Always true in Phase 3. Kept explicit so consumers cannot forget.
    is_synthetic_heuristic: bool = field(default=True)

    def __post_init__(self) -> None:
        if isinstance(self.total_bps, bool) or not isinstance(self.total_bps, int):
            raise TypeError("total_bps must be an integer")
        if not MIN_CONFIDENCE_BPS <= self.total_bps <= MAX_CONFIDENCE_BPS:
            raise ValueError(f"total_bps {self.total_bps} outside 0..{MAX_CONFIDENCE_BPS}")


def _clamp(value: int) -> int:
    return max(MIN_CONFIDENCE_BPS, min(MAX_CONFIDENCE_BPS, value))


def _finalise(risk_type: str, components: list[tuple[str, int]]) -> ConfidenceBreakdown:
    total = _clamp(sum(weight for _, weight in components))
    return ConfidenceBreakdown(
        risk_type=risk_type,
        total_bps=total,
        components=tuple(components),
    )


def _gap_penalty(timeline: OrderTimeline) -> int:
    penalty = timeline.integrity.inferred_gaps * PER_INFERRED_GAP_PENALTY_BPS
    return -min(penalty, MAX_INFERRED_GAP_PENALTY_BPS)


def explain_repeated_failure(timeline: OrderTimeline) -> ConfidenceBreakdown:
    components: list[tuple[str, int]] = [("base", BASE_REPEATED_FAILURE_BPS)]

    extra = max(0, len(timeline.failed_attempts) - 2)
    if extra:
        components.append(
            (
                "corroborating_failures",
                min(extra * PER_EXTRA_FAILURE_BPS, MAX_EXTRA_FAILURE_BONUS_BPS),
            )
        )

    penalty = _gap_penalty(timeline)
    if penalty:
        components.append(("missing_evidence", penalty))

    return _finalise("repeated_payment_failure", components)


def explain_checkout_abandonment(timeline: OrderTimeline) -> ConfidenceBreakdown:
    components: list[tuple[str, int]] = [("base", BASE_CHECKOUT_ABANDONMENT_BPS)]

    if timeline.has_checkout_abandoned:
        components.append(("explicit_abandonment_event", EXPLICIT_ABANDONMENT_BONUS_BPS))

    penalty = _gap_penalty(timeline)
    if penalty:
        components.append(("missing_evidence", penalty))

    return _finalise("checkout_abandonment", components)


def explain_subscription_failure(
    subscription: SubscriptionTimeline,
) -> ConfidenceBreakdown:
    components: list[tuple[str, int]] = [("base", BASE_SUBSCRIPTION_FAILURE_BPS)]

    extra = max(0, subscription.trailing_failure_streak - 2)
    if extra:
        components.append(
            (
                "corroborating_failures",
                min(extra * PER_EXTRA_FAILURE_BPS, MAX_EXTRA_FAILURE_BONUS_BPS),
            )
        )

    if subscription.is_halted:
        components.append(("subscription_halted", SUBSCRIPTION_HALTED_BONUS_BPS))

    return _finalise("subscription_payment_failure", components)


def explain_reconciliation(timeline: OrderTimeline) -> ConfidenceBreakdown:
    components: list[tuple[str, int]] = [("base", BASE_RECONCILIATION_BPS)]

    penalty = _gap_penalty(timeline)
    if penalty:
        components.append(("missing_evidence", penalty))

    return _finalise("reconciliation_mismatch", components)


# -- convenience accessors ------------------------------------------------


def confidence_repeated_failure(timeline: OrderTimeline) -> int:
    return explain_repeated_failure(timeline).total_bps


def confidence_checkout_abandonment(timeline: OrderTimeline) -> int:
    return explain_checkout_abandonment(timeline).total_bps


def confidence_subscription_failure(subscription: SubscriptionTimeline) -> int:
    return explain_subscription_failure(subscription).total_bps


def confidence_reconciliation(timeline: OrderTimeline) -> int:
    return explain_reconciliation(timeline).total_bps


__all__ = [
    "MAX_CONFIDENCE_BPS",
    "MIN_CONFIDENCE_BPS",
    "ConfidenceBreakdown",
    "confidence_checkout_abandonment",
    "confidence_reconciliation",
    "confidence_repeated_failure",
    "confidence_subscription_failure",
    "explain_checkout_abandonment",
    "explain_reconciliation",
    "explain_repeated_failure",
    "explain_subscription_failure",
]
