"""Checkout abandonment — specification Scenario B.

The signal here is an *absence*: a checkout began, an order exists, and no
payment was ever attempted.

Abandonment is recognised two ways, and the distinction matters:

* an explicit `checkout.abandoned` event — the provider told us, so we know
* prolonged silence past `as_of` — we inferred it, so confidence is lower

Silence is only meaningful relative to an instant, and that instant is supplied
as `as_of` rather than read from a clock. A checkout that started thirty seconds
ago has not been abandoned; it is simply in progress.
"""

from __future__ import annotations

from datetime import datetime

from app.engine.detectors.base import RiskFinding, evidence_ids, seconds_between
from app.engine.detectors.config import DEFAULT_CONFIG, DetectorConfig
from app.engine.risk_engine import amount_at_risk_checkout_abandonment, resolve_currency
from app.engine.scoring import confidence_checkout_abandonment
from app.models.enums import EventType, RiskType
from app.services.tracing.state import OrderTimeline

DETECTION_RULE = "checkout_abandonment.v1"


def detect(
    timeline: OrderTimeline,
    as_of: datetime,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> tuple[RiskFinding, ...]:
    """Detect a checkout that started and never attempted payment."""
    # An order that was paid is not abandoned, whatever else happened.
    if timeline.reached_terminal_success:
        return ()

    # Any payment attempt at all means this is a payment problem, not an
    # abandonment. The repeated-failure detector owns that case.
    if timeline.attempts:
        return ()

    if not (timeline.has_checkout_started or timeline.events):
        return ()

    last_activity = timeline.last_occurred_at
    if last_activity is None:
        return ()

    silent_for = seconds_between(last_activity, as_of)

    if timeline.has_checkout_abandoned:
        reason = "Checkout started and abandoned with no payment attempt."
    elif silent_for >= config.abandonment_silence_seconds:
        reason = (
            f"Checkout started with no payment attempt and no activity for {silent_for} seconds."
        )
    else:
        # Still in progress. Not yet abandoned.
        return ()

    return (
        RiskFinding(
            risk_type=RiskType.CHECKOUT_ABANDONMENT.value,
            merchant_id=timeline.merchant_id,
            order_id=timeline.order_id,
            customer_id=timeline.customer_id,
            order_ref=timeline.order_ref,
            amount_at_risk=amount_at_risk_checkout_abandonment(timeline),
            currency=resolve_currency(timeline),
            confidence_bps=confidence_checkout_abandonment(timeline),
            detection_rule=DETECTION_RULE,
            reason=reason,
            evidence_event_ids=evidence_ids(
                timeline.events,
                (
                    EventType.CHECKOUT_STARTED.value,
                    EventType.CHECKOUT_ABANDONED.value,
                    EventType.ORDER_CREATED.value,
                ),
            ),
            detected_at=as_of,
        ),
    )
