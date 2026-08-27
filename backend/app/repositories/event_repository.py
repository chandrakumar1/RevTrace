"""Event persistence and retrieval.

Idempotency is delegated to the database. `UNIQUE(merchant_id, external_event_id)`
from the Phase 1 schema is the enforcement point, and `ON CONFLICT DO NOTHING`
lets a redelivered webhook be *offered* to storage and silently declined —
which is exactly the behaviour the Phase 2 duplicate scenario exists to prove.

`events` is append-only. Nothing in this module updates or deletes a row.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Event


def insert_events(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    """Insert events, suppressing duplicates. Returns rows actually written.

    `on_conflict_do_nothing()` is used without a constraint target so that a
    repeat of either the primary key or the (merchant_id, external_event_id)
    idempotency key is handled identically.
    """
    if not rows:
        return 0

    statement = insert(Event).values(list(rows)).on_conflict_do_nothing()
    result = session.execute(statement.returning(Event.id))
    return len(result.fetchall())


def events_for_order(session: Session, order_id: uuid.UUID) -> list[Event]:
    """All events for one order, in causal order.

    Ordered by `occurred_at` and tie-broken by `external_event_id` so the
    sequence is total and stable. Never ordered by arrival or insertion.
    """
    statement = (
        select(Event)
        .where(Event.order_id == order_id)
        .order_by(Event.occurred_at, Event.external_event_id)
    )
    return list(session.execute(statement).scalars())


def events_for_merchant(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    include_orderless: bool = True,
) -> list[Event]:
    """All events for one merchant, in causal order.

    Subscription events carry no order_id, so `include_orderless` keeps them in
    scope for the subscription detector.
    """
    statement = select(Event).where(Event.merchant_id == merchant_id)
    if not include_orderless:
        statement = statement.where(Event.order_id.is_not(None))

    statement = statement.order_by(Event.occurred_at, Event.external_event_id)
    return list(session.execute(statement).scalars())


def stored_external_event_ids(
    session: Session, merchant_id: uuid.UUID, external_ids: Sequence[str]
) -> set[str]:
    """Which of these external event ids the merchant already has."""
    if not external_ids:
        return set()

    statement = select(Event.external_event_id).where(
        Event.merchant_id == merchant_id,
        Event.external_event_id.in_(list(external_ids)),
    )
    return {row for row in session.execute(statement).scalars() if row is not None}
