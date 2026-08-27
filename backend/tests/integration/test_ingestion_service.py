"""Ingestion against the real database.

Runs on revtrace_test inside a rolled-back transaction. The duplicate-suppression
tests are the point: they prove the Phase 1 UNIQUE(merchant_id, external_event_id)
constraint is what enforces idempotency, not application-side filtering.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditEvent,
    Customer,
    Event,
    Merchant,
    Order,
    PaymentAttempt,
    RecoveryAction,
    RecoveryCase,
    RevenueRisk,
)
from app.schemas.ingestion import SimulationIngestRequest
from app.services.ingestion.errors import UnknownEntityReferenceError
from app.services.ingestion.service import ingest_simulation
from tests.ingestion.conftest import ingest_payload

pytestmark = pytest.mark.db


def _ingest(session: Session, scenario: str, seed: int = 42):
    request = SimulationIngestRequest.model_validate(ingest_payload(scenario, seed))
    return request, ingest_simulation(session, request)


class TestCleanIngestion:
    def test_healthy_payment_ingests(self, db_session: Session) -> None:
        request, result = _ingest(db_session, "S01")

        assert result.merchants_upserted == 1
        assert result.orders_upserted == 1
        assert result.events_received == len(request.deliveries)
        assert result.events_persisted == len(request.deliveries)
        assert result.duplicates_suppressed == 0

    def test_rows_actually_land(self, db_session: Session) -> None:
        request, _ = _ingest(db_session, "S04")
        db_session.flush()

        assert db_session.scalar(select(func.count()).select_from(Merchant)) == 1
        assert db_session.scalar(select(func.count()).select_from(Order)) == 1
        assert db_session.scalar(select(func.count()).select_from(PaymentAttempt)) == 3
        assert db_session.scalar(select(func.count()).select_from(Event)) == len(request.deliveries)

    def test_manifest_metadata_is_echoed(self, db_session: Session) -> None:
        _, result = _ingest(db_session, "S04")
        assert result.scenario_id == "S04"
        assert result.seed == 42

    def test_occurred_and_received_are_both_preserved(self, db_session: Session) -> None:
        """received_at must not be overwritten with server time on ingest."""
        request, _ = _ingest(db_session, "S09")
        db_session.flush()

        submitted = {d.event.external_event_id: d.event for d in request.deliveries}
        for event in db_session.execute(select(Event)).scalars():
            original = submitted[event.external_event_id]
            assert event.occurred_at == original.occurred_at
            assert event.received_at == original.received_at

    def test_delay_signal_survives(self, db_session: Session) -> None:
        _, _ = _ingest(db_session, "S09")
        db_session.flush()

        lags = [
            (e.received_at - e.occurred_at).total_seconds()
            for e in db_session.execute(select(Event)).scalars()
        ]
        assert max(lags) >= 6 * 60 * 60

    def test_subscription_events_persist_without_an_order(self, db_session: Session) -> None:
        _, result = _ingest(db_session, "S06")
        db_session.flush()

        assert result.orders_upserted == 0
        assert result.events_persisted > 0
        assert all(e.order_id is None for e in db_session.execute(select(Event)).scalars())

    def test_large_scenario_ingests(self, db_session: Session) -> None:
        request, result = _ingest(db_session, "S14")
        assert result.events_persisted == len(request.deliveries)
        assert result.orders_upserted == 20


class TestDuplicateSuppression:
    """The database is the enforcement point, not the application."""

    def test_duplicates_are_offered_and_declined(self, db_session: Session) -> None:
        request, result = _ingest(db_session, "S07")

        assert result.events_received == len(request.deliveries)
        assert result.events_persisted < result.events_received
        assert result.duplicates_suppressed > 0

    def test_persisted_count_matches_unique_external_ids(self, db_session: Session) -> None:
        request, result = _ingest(db_session, "S07")
        unique = {d.event.external_event_id for d in request.deliveries}
        assert result.events_persisted == len(unique)

    def test_no_double_counted_rows(self, db_session: Session) -> None:
        request, _ = _ingest(db_session, "S07")
        db_session.flush()

        unique = {d.event.external_event_id for d in request.deliveries}
        assert db_session.scalar(select(func.count()).select_from(Event)) == len(unique)


class TestIdempotency:
    def test_reingesting_persists_nothing_new(self, db_session: Session) -> None:
        request = SimulationIngestRequest.model_validate(ingest_payload("S04"))

        first = ingest_simulation(db_session, request)
        db_session.flush()
        second = ingest_simulation(db_session, request)
        db_session.flush()

        assert first.events_persisted > 0
        assert second.events_persisted == 0
        assert second.duplicates_suppressed == second.events_received

    def test_reingesting_leaves_row_counts_unchanged(self, db_session: Session) -> None:
        request = SimulationIngestRequest.model_validate(ingest_payload("S04"))

        ingest_simulation(db_session, request)
        db_session.flush()
        before = db_session.scalar(select(func.count()).select_from(Event))

        ingest_simulation(db_session, request)
        db_session.flush()
        assert db_session.scalar(select(func.count()).select_from(Event)) == before

    def test_entities_are_never_overwritten(self, db_session: Session) -> None:
        request = SimulationIngestRequest.model_validate(ingest_payload("S04"))
        ingest_simulation(db_session, request)
        db_session.flush()

        order = db_session.execute(select(Order)).scalars().one()
        original_status = order.status

        ingest_simulation(db_session, request)
        db_session.flush()
        db_session.refresh(order)
        assert order.status == original_status

    def test_three_ingests_are_still_idempotent(self, db_session: Session) -> None:
        request = SimulationIngestRequest.model_validate(ingest_payload("S01"))
        counts = []
        for _ in range(3):
            counts.append(ingest_simulation(db_session, request).events_persisted)
            db_session.flush()
        assert counts[1:] == [0, 0]


class TestReferenceValidation:
    def test_event_with_unknown_merchant_is_refused(self, db_session: Session) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["merchants"] = []
        request = SimulationIngestRequest.model_validate(payload)

        with pytest.raises(UnknownEntityReferenceError, match="unknown merchants"):
            ingest_simulation(db_session, request)

    def test_event_with_unknown_order_is_refused(self, db_session: Session) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["orders"] = []
        request = SimulationIngestRequest.model_validate(payload)

        with pytest.raises(UnknownEntityReferenceError, match="unknown orders"):
            ingest_simulation(db_session, request)

    def test_nothing_is_written_when_references_fail(self, db_session: Session) -> None:
        payload = ingest_payload("S01")
        payload["entities"]["merchants"] = []
        request = SimulationIngestRequest.model_validate(payload)

        with pytest.raises(UnknownEntityReferenceError):
            ingest_simulation(db_session, request)

        # The reference check runs before any write, so the session is still
        # usable and nothing needs rolling back here — the fixture handles that.
        assert db_session.scalar(select(func.count()).select_from(Event)) == 0

    def test_previously_stored_merchant_satisfies_the_reference(self, db_session: Session) -> None:
        request = SimulationIngestRequest.model_validate(ingest_payload("S01"))
        ingest_simulation(db_session, request)
        db_session.flush()

        events_only = SimulationIngestRequest.model_validate(
            {
                "entities": {
                    "merchants": [],
                    "customers": [],
                    "orders": [],
                    "payment_attempts": [],
                },
                "deliveries": [d.model_dump(mode="json") for d in request.deliveries],
            }
        )
        result = ingest_simulation(db_session, events_only)
        assert result.duplicates_suppressed == result.events_received


class TestAuthorityBoundary:
    """Ingestion may only ever write five tables."""

    def test_no_risk_or_recovery_rows_are_created(self, db_session: Session) -> None:
        for scenario in ("S01", "S04", "S07", "S10", "S11", "S12"):
            _ingest(db_session, scenario)
        db_session.flush()

        for model in (RevenueRisk, RecoveryCase, RecoveryAction, AuditEvent):
            assert db_session.scalar(select(func.count()).select_from(model)) == 0

    def test_only_expected_entity_tables_are_populated(self, db_session: Session) -> None:
        _ingest(db_session, "S04")
        db_session.flush()

        for model in (Merchant, Customer, Order, PaymentAttempt, Event):
            assert db_session.scalar(select(func.count()).select_from(model)) > 0
