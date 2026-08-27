"""Entity persistence for ingestion.

Upserts are `ON CONFLICT DO NOTHING` on the primary key: re-ingesting a fixture
is a no-op rather than an overwrite. Nothing here ever updates an existing row,
so an ingest can never rewrite history that detection has already reasoned over.

Order state is deliberately not refreshed from the entity payload either —
Phase 3 derives order state from the event timeline, never from `orders.status`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Customer, Merchant, Order, PaymentAttempt


def _insert_ignoring_conflicts(
    session: Session, model: type[Any], rows: Sequence[dict[str, Any]]
) -> int:
    """Insert rows, skipping any that already exist. Returns rows written."""
    if not rows:
        return 0

    statement = insert(model).values(list(rows)).on_conflict_do_nothing()
    result = session.execute(statement.returning(model.id))
    return len(result.fetchall())


def upsert_merchants(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _insert_ignoring_conflicts(session, Merchant, rows)


def upsert_customers(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _insert_ignoring_conflicts(session, Customer, rows)


def upsert_orders(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _insert_ignoring_conflicts(session, Order, rows)


def upsert_payment_attempts(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _insert_ignoring_conflicts(session, PaymentAttempt, rows)


def existing_ids(session: Session, model: type[Any], ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these primary keys are already stored."""
    if not ids:
        return set()
    rows = session.execute(select(model.id).where(model.id.in_(list(ids)))).scalars()
    return set(rows)
