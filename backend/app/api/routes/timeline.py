"""Order timeline endpoint.

Serves the reconstructed causal timeline alongside its delivery forensics. The
frontend renders the causal sequence and badges the anomalies.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import DbSession
from app.api.routes.risks import _attempts_out, _integrity_out, _money_out
from app.repositories import event_repository
from app.schemas.timeline import OrderTimelineResponse, TimelineEntry
from app.services.tracing.reconstruction import reconstruct_order, timeline_view

router = APIRouter(prefix="/orders", tags=["timeline"])

_SUMMARIES = {
    "order.created": "Order created",
    "order.paid": "Order paid",
    "checkout.started": "Checkout started",
    "checkout.abandoned": "Checkout abandoned",
    "payment.attempted": "Payment attempted",
    "payment.authorized": "Payment authorized",
    "payment.captured": "Payment captured",
    "payment.failed": "Payment failed",
    "subscription.charged": "Subscription charged",
    "subscription.payment_failed": "Subscription payment failed",
    "subscription.halted": "Subscription halted",
    "refund.created": "Refund created",
    "recovery.action_executed": "Recovery action executed",
    "recovery.succeeded": "Recovery succeeded",
    "recovery.failed": "Recovery failed",
}


def _summarize(event_type: str, failure_code: object) -> str:
    base = _SUMMARIES.get(event_type, event_type)
    if isinstance(failure_code, str) and failure_code:
        return f"{base} — {failure_code}"
    return base


@router.get(
    "/{order_id}/timeline",
    response_model=OrderTimelineResponse,
    summary="Reconstructed causal timeline for one order",
    description=(
        "Entries are ordered causally, by `occurred_at`. `delivery_position` "
        "records where each event sat in arrival order, so out-of-order and "
        "delayed delivery stay visible without distorting the sequence."
    ),
)
def get_order_timeline(order_id: uuid.UUID, session: DbSession) -> OrderTimelineResponse:
    events = event_repository.events_for_order(session, order_id)
    if not events:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no events found for order {order_id}",
        )

    timeline = reconstruct_order(order_id, events)
    rows = timeline_view(timeline)
    by_id = {event.external_event_id: event for event in timeline.events}

    entries = [
        TimelineEntry(
            causal_position=row["causal_position"],
            delivery_position=row["delivery_position"],
            external_event_id=row["external_event_id"],
            event_type=row["event_type"],
            occurred_at=row["occurred_at"],
            received_at=row["received_at"],
            delay_seconds=row["delay_seconds"],
            summary=_summarize(
                row["event_type"],
                by_id[row["external_event_id"]].payload.get("failure_code")
                if row["external_event_id"] in by_id
                else None,
            ),
        )
        for row in rows
    ]

    return OrderTimelineResponse(
        order_id=timeline.order_id,
        merchant_id=timeline.merchant_id,
        customer_id=timeline.customer_id,
        order_ref=timeline.order_ref,
        state=timeline.state,
        currency=timeline.currency,
        money=_money_out(timeline),
        reached_terminal_success=timeline.reached_terminal_success,
        has_capture=timeline.has_capture,
        has_order_paid=timeline.has_order_paid,
        has_refund=timeline.has_refund,
        entries=entries,
        attempts=_attempts_out(timeline),
        integrity=_integrity_out(timeline),
        events_examined=len(timeline.events),
    )
