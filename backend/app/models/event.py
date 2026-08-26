"""Event — the append-only spine of the Revenue Leak Graph.

Three columns carry the webhook-tolerance requirements from the specification:

* ``external_event_id`` + UNIQUE(merchant_id, external_event_id) makes ingestion
  idempotent. Duplicate delivery is rejected at the storage layer, so a
  redelivered webhook cannot double-count revenue or corrupt the timeline.
* ``occurred_at`` is when the thing happened; ``received_at`` is when we saw it.
  Out-of-order and delayed delivery are only detectable with both.

Timelines are always reconstructed by ``occurred_at``, never by insertion order.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import EventType
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, enum_check


class Event(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only. Never updated in place."""

    __tablename__ = "events"

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    #: Provider/simulator event identifier. The idempotency key.
    external_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    #: When the event actually happened.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    #: When RevTrace received it. Later than occurred_at for delayed delivery.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "merchant_id", "external_event_id", name="uq_events_merchant_external_event"
        ),
        enum_check("event_type", EventType.values(), name="event_type_valid"),
        # Timeline reconstruction for one order, in occurrence order.
        Index("ix_events_order_occurred", "order_id", "occurred_at"),
        # Detection sweeps scan a merchant's recent window by type.
        Index("ix_events_merchant_type_occurred", "merchant_id", "event_type", "occurred_at"),
    )
