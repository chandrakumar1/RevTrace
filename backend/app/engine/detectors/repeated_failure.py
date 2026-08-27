"""Repeated payment failure — specification Scenario A.

Fires when an order accumulated two or more failed attempts within a window and
was never collected.

The suppression rule is the important half. `reached_terminal_success` is
checked against the whole causal timeline, so a success that *occurred after*
the failures still suppresses the finding. That is what separates this detector
from one that reports every customer who ever had a card declined:

* S02 — one failure, then an organic retry that succeeded. Not a leak.
* S03 — two failures, then success on the third try. Not a leak.
* S11 — failures, a recovery action, then payment. Not a standing leak.

Amount at risk is the ORDER amount, counted once (see `risk_engine`).
"""

from __future__ import annotations

from datetime import datetime

from app.engine.detectors.base import RiskFinding, evidence_ids, seconds_between
from app.engine.detectors.config import DEFAULT_CONFIG, DetectorConfig
from app.engine.risk_engine import amount_at_risk_repeated_failure, resolve_currency
from app.engine.scoring import confidence_repeated_failure
from app.models.enums import EventType, RiskType
from app.services.tracing.state import OrderTimeline

DETECTION_RULE = "repeated_payment_failure.v1"


def detect(
    timeline: OrderTimeline,
    as_of: datetime,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> tuple[RiskFinding, ...]:
    """Detect unrecovered repeated payment failure on one order."""
    # Suppression first: an order that was ultimately paid is not a leak,
    # however many attempts it took to get there.
    if timeline.reached_terminal_success:
        return ()

    failures = timeline.failed_attempts
    if len(failures) < config.min_failed_attempts:
        return ()

    # Failures must cluster. Two declines months apart are two separate
    # purchases, not one struggling checkout.
    span = seconds_between(failures[0].first_seen_at, failures[-1].last_seen_at)
    if span > config.failure_window_seconds:
        return ()

    codes = sorted({f.failure_code for f in failures if f.failure_code})
    reason = f"{len(failures)} failed payment attempts with no successful payment" + (
        f"; failure_code={', '.join(codes)}" if codes else ""
    )
    if timeline.integrity.inferred_gaps:
        reason += (
            f"; {timeline.integrity.inferred_gaps} attempt event(s) never delivered "
            "and were inferred from later evidence"
        )

    return (
        RiskFinding(
            risk_type=RiskType.REPEATED_PAYMENT_FAILURE.value,
            merchant_id=timeline.merchant_id,
            order_id=timeline.order_id,
            customer_id=timeline.customer_id,
            order_ref=timeline.order_ref,
            amount_at_risk=amount_at_risk_repeated_failure(timeline),
            currency=resolve_currency(timeline),
            confidence_bps=confidence_repeated_failure(timeline),
            detection_rule=DETECTION_RULE,
            reason=reason,
            evidence_event_ids=evidence_ids(
                timeline.events,
                (EventType.PAYMENT_FAILED.value, EventType.PAYMENT_ATTEMPTED.value),
            ),
            detected_at=as_of,
        ),
    )
