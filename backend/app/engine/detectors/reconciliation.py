"""Reconciliation mismatch — Phase 3 addition, scenario S10.

Money was captured but the order never reconciled to paid. Every event that
occurred was delivered; the terminal `order.paid` genuinely never happened.

**Amount at risk is always zero.** The funds arrived. What is broken is
bookkeeping, not revenue, and reporting the order amount here would inflate
every at-risk total in the product with money that was actually collected. The
captured figure stays visible in the evidence instead.

The grace period is the guard that separates this from a delayed webhook. An
`order.paid` still in flight is not a mismatch — it is a late event, and the
specification is explicit that late delivery must be tolerated. Only after the
grace period elapses (measured against the supplied `as_of`, never a clock) is
the absence treated as real.
"""

from __future__ import annotations

from datetime import datetime

from app.engine.detectors.base import RiskFinding, evidence_ids, seconds_between
from app.engine.detectors.config import DEFAULT_CONFIG, DetectorConfig
from app.engine.risk_engine import (
    amount_at_risk_reconciliation,
    captured_amount,
    resolve_currency,
)
from app.engine.scoring import confidence_reconciliation
from app.models.enums import EventType, RiskType
from app.services.tracing.state import OrderTimeline

DETECTION_RULE = "reconciliation_mismatch.v1"


def _last_capture_at(timeline: OrderTimeline) -> datetime | None:
    captures = [
        event.occurred_at
        for event in timeline.events
        if event.event_type == EventType.PAYMENT_CAPTURED.value
    ]
    return max(captures) if captures else None


def detect(
    timeline: OrderTimeline,
    as_of: datetime,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> tuple[RiskFinding, ...]:
    """Detect a captured payment whose order never reconciled."""
    if not timeline.has_capture:
        return ()

    # The order did reconcile. Nothing is wrong.
    if timeline.has_order_paid:
        return ()

    # A refunded order is a deliberate reversal, not a bookkeeping failure.
    if timeline.has_refund:
        return ()

    captured_at = _last_capture_at(timeline)
    if captured_at is None:
        return ()

    waited = seconds_between(captured_at, as_of)
    if waited < config.reconciliation_grace_seconds:
        # order.paid may still be in flight. A late event is not a missing one.
        return ()

    captured = captured_amount(timeline)

    return (
        RiskFinding(
            risk_type=RiskType.RECONCILIATION_MISMATCH.value,
            merchant_id=timeline.merchant_id,
            order_id=timeline.order_id,
            customer_id=timeline.customer_id,
            order_ref=timeline.order_ref,
            # Always zero: the money arrived. This is an integrity anomaly.
            amount_at_risk=amount_at_risk_reconciliation(timeline),
            currency=resolve_currency(timeline),
            confidence_bps=confidence_reconciliation(timeline),
            detection_rule=DETECTION_RULE,
            reason=(
                f"Payment captured ({captured} minor units) but order.paid never arrived; "
                f"order still in state {timeline.state!r} after {waited} seconds. "
                "No revenue is at risk — the funds were collected — but the order "
                "has not reconciled."
            ),
            evidence_event_ids=evidence_ids(
                timeline.events,
                (EventType.PAYMENT_CAPTURED.value, EventType.PAYMENT_AUTHORIZED.value),
            ),
            detected_at=as_of,
        ),
    )
