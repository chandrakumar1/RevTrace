"""Subscription payment failure — specification Scenario C.

Recurring revenue has stopped: consecutive billing cycles failed and none
succeeded afterwards.

This detector operates on a `SubscriptionTimeline` rather than an order, because
subscription events carry no `order_id` in the Phase 1 schema and are grouped by
the `subscription_ref` in their payload.

The suppression signal is the *trailing* failure streak, not the total failure
count. A subscription that failed twice a year ago and has billed successfully
every month since is healthy — reconstruction resets the streak on each
successful charge, so a later success suppresses the finding exactly as a
successful retry does for a one-off order.

Amount at risk sums the failed cycles. That is correct here and would be wrong
for an order: each failed cycle is a separate charge that never happened, not
repeated attempts at collecting one debt.
"""

from __future__ import annotations

from datetime import datetime

from app.engine.detectors.base import RiskFinding, evidence_ids
from app.engine.detectors.config import DEFAULT_CONFIG, DetectorConfig
from app.engine.risk_engine import amount_at_risk_subscription
from app.engine.scoring import confidence_subscription_failure
from app.models.enums import EventType, RiskType
from app.services.tracing.state import SubscriptionTimeline

DETECTION_RULE = "subscription_payment_failure.v1"


def detect(
    subscription: SubscriptionTimeline,
    as_of: datetime,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> tuple[RiskFinding, ...]:
    """Detect recurring revenue that has stopped."""
    # A success after the failures resets the streak, which is what makes a
    # recovered subscription silent here.
    if subscription.trailing_failure_streak < config.min_subscription_failures:
        return ()

    amount = amount_at_risk_subscription(subscription)
    if amount <= 0:
        return ()

    reason = (
        f"{subscription.trailing_failure_streak} consecutive failed billing cycles "
        f"with no subsequent successful charge"
    )
    if subscription.is_halted:
        reason += "; subscription halted"

    return (
        RiskFinding(
            risk_type=RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value,
            merchant_id=subscription.merchant_id,
            # Subscription events carry no order in the Phase 1 schema.
            order_id=None,
            customer_id=subscription.customer_id,
            order_ref=subscription.subscription_ref,
            amount_at_risk=amount,
            currency=subscription.currency,
            confidence_bps=confidence_subscription_failure(subscription),
            detection_rule=DETECTION_RULE,
            reason=reason,
            evidence_event_ids=evidence_ids(
                subscription.events,
                (
                    EventType.SUBSCRIPTION_PAYMENT_FAILED.value,
                    EventType.SUBSCRIPTION_HALTED.value,
                ),
            ),
            detected_at=as_of,
        ),
    )
