"""Order timeline response schemas.

Two orderings travel together and are never conflated: `causal_position` is what
actually happened, `delivery_position` is the order it reached us. The frontend
renders the causal timeline and badges the delivery anomalies.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime
from app.schemas.risk import AttemptOut, IntegrityOut, MoneyOut


class TimelineEntry(BaseModel):
    #: Position when sorted by occurrence. This is the true sequence.
    causal_position: int
    #: Position when sorted by arrival. Differs under out-of-order delivery.
    delivery_position: int | None
    external_event_id: str | None
    event_type: str
    occurred_at: UtcDatetime
    received_at: UtcDatetime
    delay_seconds: int
    summary: str


class OrderTimelineResponse(BaseModel):
    order_id: uuid.UUID
    merchant_id: uuid.UUID
    customer_id: uuid.UUID | None
    order_ref: str | None
    #: Derived from events, never read from orders.status.
    state: str
    currency: str | None
    money: MoneyOut
    reached_terminal_success: bool
    has_capture: bool
    has_order_paid: bool
    has_refund: bool
    entries: list[TimelineEntry] = Field(default_factory=list)
    attempts: list[AttemptOut] = Field(default_factory=list)
    integrity: IntegrityOut
    events_examined: int
