"""Detector registry.

Detectors are pure functions. Nothing here opens a database connection, makes a
network call, or reads a clock — the evaluation instant always arrives as
`as_of`.

`payment_degradation` is deliberately absent. Phase 2 has no scenario that
produces a merchant-wide degradation, so a detector for it would ship with no
positive control and therefore no evidence that it works. It is deferred until
a properly designed scenario exists.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from app.engine.detectors import abandonment, reconciliation, repeated_failure, subscription
from app.engine.detectors.base import RiskFinding
from app.engine.detectors.config import DEFAULT_CONFIG, DetectorConfig
from app.services.tracing.state import MerchantTimeline, OrderTimeline, SubscriptionTimeline

OrderDetector = Callable[[OrderTimeline, datetime, DetectorConfig], tuple[RiskFinding, ...]]
SubscriptionDetector = Callable[
    [SubscriptionTimeline, datetime, DetectorConfig], tuple[RiskFinding, ...]
]

#: Order-scoped detectors, applied to every reconstructed order timeline.
ORDER_DETECTORS: tuple[OrderDetector, ...] = (
    repeated_failure.detect,
    abandonment.detect,
    reconciliation.detect,
)

#: Subscription-scoped detectors.
SUBSCRIPTION_DETECTORS: tuple[SubscriptionDetector, ...] = (subscription.detect,)

#: Every rule identifier this build can emit, for audit and documentation.
DETECTION_RULES: tuple[str, ...] = (
    repeated_failure.DETECTION_RULE,
    abandonment.DETECTION_RULE,
    reconciliation.DETECTION_RULE,
    subscription.DETECTION_RULE,
)


def detect_for_order(
    timeline: OrderTimeline,
    as_of: datetime,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> tuple[RiskFinding, ...]:
    """Run every order detector. Results are ordered deterministically."""
    findings: list[RiskFinding] = []
    for detector in ORDER_DETECTORS:
        findings.extend(detector(timeline, as_of, config))
    return tuple(sorted(findings, key=lambda f: f.detection_rule))


def detect_for_subscription(
    subscription_timeline: SubscriptionTimeline,
    as_of: datetime,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> tuple[RiskFinding, ...]:
    findings: list[RiskFinding] = []
    for detector in SUBSCRIPTION_DETECTORS:
        findings.extend(detector(subscription_timeline, as_of, config))
    return tuple(sorted(findings, key=lambda f: f.detection_rule))


def detect_for_merchant(
    timeline: MerchantTimeline,
    as_of: datetime,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> tuple[RiskFinding, ...]:
    """Every finding for one merchant, in a stable order."""
    findings: list[RiskFinding] = []

    for order in timeline.orders:
        findings.extend(detect_for_order(order, as_of, config))
    for subscription_timeline in timeline.subscriptions:
        findings.extend(detect_for_subscription(subscription_timeline, as_of, config))

    return tuple(
        sorted(findings, key=lambda f: (str(f.order_id or ""), f.detection_rule, f.order_ref or ""))
    )


def findings_for(
    timelines: Sequence[OrderTimeline],
    as_of: datetime,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> tuple[RiskFinding, ...]:
    findings: list[RiskFinding] = []
    for timeline in timelines:
        findings.extend(detect_for_order(timeline, as_of, config))
    return tuple(findings)


__all__ = [
    "DETECTION_RULES",
    "ORDER_DETECTORS",
    "SUBSCRIPTION_DETECTORS",
    "DetectorConfig",
    "RiskFinding",
    "detect_for_merchant",
    "detect_for_order",
    "detect_for_subscription",
    "findings_for",
]
