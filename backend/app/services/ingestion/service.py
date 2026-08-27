"""Ingestion orchestration.

Writes entities in foreign-key order, then events. Everything runs in the
caller's transaction, so a failure anywhere leaves nothing behind.

This module can reach exactly five tables: merchants, customers, orders,
payment_attempts, events. It has no import path to revenue_risks,
recovery_cases, recovery_actions, or audit_events — simulated input can
therefore never manufacture a risk or an approval. A test asserts the absence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Merchant, Order
from app.repositories import entity_repository, event_repository
from app.schemas.ingestion import SimulationIngestRequest
from app.services.ingestion.errors import UnknownEntityReferenceError


@dataclass(frozen=True, slots=True)
class IngestionResult:
    merchants_upserted: int = 0
    customers_upserted: int = 0
    orders_upserted: int = 0
    payment_attempts_upserted: int = 0
    events_received: int = 0
    events_persisted: int = 0
    duplicates_suppressed: int = 0
    scenario_id: str | None = None
    seed: int | None = None


def _merchant_payload(request: SimulationIngestRequest) -> list[dict[str, object]]:
    # external_ref has no column in the Phase 1 merchants table; drop it.
    return [
        {
            "id": m.id,
            "name": m.name,
            "currency": m.currency,
            "timezone": m.timezone,
        }
        for m in request.entities.merchants
    ]


def _validate_references(session: Session, request: SimulationIngestRequest) -> None:
    """Fail before writing if an event points at an unknown merchant or order.

    `events.merchant_id` is NOT NULL with a foreign key. Catching it here turns
    an opaque mid-batch integrity error into a clear, pre-write rejection.
    """
    batch_merchants = {m.id for m in request.entities.merchants}
    batch_orders = {o.id for o in request.entities.orders}

    referenced_merchants = {d.event.merchant_id for d in request.deliveries}
    referenced_orders = {
        d.event.order_id for d in request.deliveries if d.event.order_id is not None
    }

    unknown_merchants = referenced_merchants - batch_merchants
    if unknown_merchants:
        unknown_merchants -= entity_repository.existing_ids(
            session, Merchant, sorted(unknown_merchants)
        )
    if unknown_merchants:
        raise UnknownEntityReferenceError(
            f"events reference unknown merchants: {sorted(str(m) for m in unknown_merchants)}"
        )

    unknown_orders = referenced_orders - batch_orders
    if unknown_orders:
        unknown_orders -= entity_repository.existing_ids(session, Order, sorted(unknown_orders))
    if unknown_orders:
        raise UnknownEntityReferenceError(
            f"events reference unknown orders: {sorted(str(o) for o in unknown_orders)}"
        )


def ingest_simulation(session: Session, request: SimulationIngestRequest) -> IngestionResult:
    """Persist a simulator fixture. Idempotent: re-ingesting is a no-op."""
    _validate_references(session, request)

    merchants = entity_repository.upsert_merchants(session, _merchant_payload(request))
    customers = entity_repository.upsert_customers(
        session, [c.model_dump() for c in request.entities.customers]
    )
    orders = entity_repository.upsert_orders(
        session, [o.model_dump() for o in request.entities.orders]
    )
    attempts = entity_repository.upsert_payment_attempts(
        session, [a.model_dump() for a in request.entities.payment_attempts]
    )

    # The delivery envelope is intentionally discarded: only the event is a row.
    # Duplicates are offered to the database and declined by the unique
    # constraint rather than filtered out here.
    event_rows = [d.event.model_dump() for d in request.deliveries]
    persisted = event_repository.insert_events(session, event_rows)

    manifest = request.manifest or {}
    seed = manifest.get("seed")

    return IngestionResult(
        merchants_upserted=merchants,
        customers_upserted=customers,
        orders_upserted=orders,
        payment_attempts_upserted=attempts,
        events_received=len(event_rows),
        events_persisted=persisted,
        duplicates_suppressed=len(event_rows) - persisted,
        scenario_id=manifest.get("scenario_id"),
        seed=seed if isinstance(seed, int) else None,
    )


def distinct_merchant_ids(request: SimulationIngestRequest) -> list[uuid.UUID]:
    """Merchants touched by a request, for a follow-up detection run."""
    return sorted({m.id for m in request.entities.merchants})
