"""Detection persistence against the real database.

Runs on revtrace_test inside a rolled-back transaction. revtrace_dev is never
touched.

Two things carry this file: the upsert on `(merchant_id, order_id, risk_type)`
must make a re-run idempotent, and detection must be structurally incapable of
writing recovery or audit rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditEvent, RecoveryAction, RecoveryCase, RevenueRisk
from app.models.enums import RiskStatus, RiskType
from app.repositories import risk_repository
from app.schemas.ingestion import SimulationIngestRequest
from app.services.detection.service import run_detection
from app.services.ingestion.service import ingest_simulation
from tests.ingestion.conftest import ingest_payload

pytestmark = pytest.mark.db

AS_OF = datetime(2026, 6, 1, tzinfo=UTC)

#: Scenarios that must persist exactly one risk.
LEAK_SCENARIOS = ("S04", "S04b", "S04c", "S05", "S06", "S08", "S09", "S10", "S12", "S12b")

#: Scenarios that must persist nothing at all.
CLEAN_SCENARIOS = ("S01", "S02", "S03", "S07", "S11", "S13", "S14")


def _ingest(session: Session, scenario: str, seed: int = 42) -> uuid.UUID:
    request = SimulationIngestRequest.model_validate(ingest_payload(scenario, seed))
    ingest_simulation(session, request)
    session.flush()
    return request.entities.merchants[0].id


def _stored(session: Session, merchant_id: uuid.UUID) -> list[RevenueRisk]:
    return risk_repository.risks_for_merchant(session, merchant_id)


class TestNewFindingsArePersisted:
    @pytest.mark.parametrize("scenario", LEAK_SCENARIOS)
    def test_one_risk_per_leak_scenario(self, db_session: Session, scenario: str) -> None:
        session = db_session
        merchant_id = _ingest(session, scenario)
        summary = run_detection(session, merchant_id, AS_OF)

        assert summary.risks_created == 1
        assert len(_stored(session, merchant_id)) == 1

    def test_stored_row_matches_the_finding(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)

        risk = _stored(db_session, merchant_id)[0]
        assert risk.risk_type == RiskType.REPEATED_PAYMENT_FAILURE.value
        assert risk.status == RiskStatus.DETECTED.value
        assert risk.amount_at_risk > 0
        assert risk.currency == "INR"
        assert risk.detection_rule == "repeated_payment_failure.v1"

    def test_detected_at_is_the_supplied_instant(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)
        assert _stored(db_session, merchant_id)[0].detected_at == AS_OF

    def test_is_true_positive_stays_null(self, db_session: Session) -> None:
        """Phase 11 labels evaluation data; detection must not label itself."""
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)
        assert _stored(db_session, merchant_id)[0].is_true_positive is None

    def test_amount_is_an_integer_in_the_database(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)

        amount = _stored(db_session, merchant_id)[0].amount_at_risk
        assert isinstance(amount, int) and not isinstance(amount, bool)

    def test_reconciliation_persists_zero_at_risk(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S10")
        run_detection(db_session, merchant_id, AS_OF)

        risk = _stored(db_session, merchant_id)[0]
        assert risk.risk_type == RiskType.RECONCILIATION_MISMATCH.value
        assert risk.amount_at_risk == 0

    def test_reconciliation_mismatch_passes_the_check_constraint(self, db_session: Session) -> None:
        """The Phase 3 migration is what makes this insert legal."""
        merchant_id = _ingest(db_session, "S10")
        run_detection(db_session, merchant_id, AS_OF)
        db_session.flush()

        stored = (
            db_session.execute(
                select(RevenueRisk.risk_type).where(RevenueRisk.merchant_id == merchant_id)
            )
            .scalars()
            .all()
        )
        assert "reconciliation_mismatch" in stored

    def test_subscription_risk_has_no_order(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S06")
        run_detection(db_session, merchant_id, AS_OF)

        risk = _stored(db_session, merchant_id)[0]
        assert risk.risk_type == RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value
        assert risk.order_id is None


class TestCleanScenariosPersistNothing:
    @pytest.mark.parametrize("scenario", CLEAN_SCENARIOS)
    def test_no_rows_written(self, db_session: Session, scenario: str) -> None:
        session = db_session
        merchant_id = _ingest(session, scenario)
        summary = run_detection(session, merchant_id, AS_OF)

        assert summary.risks_created == 0
        assert _stored(session, merchant_id) == []

    def test_mixed_baseline_writes_nothing(self, db_session: Session) -> None:
        """S14: 20 orders, ~85% success, no repeated failures."""
        merchant_id = _ingest(db_session, "S14")
        summary = run_detection(db_session, merchant_id, AS_OF)

        assert summary.orders_examined == 20
        assert summary.risks_created == 0


class TestReRunIdempotence:
    """The natural-key upsert is what makes this hold."""

    def test_second_run_creates_nothing(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")

        first = run_detection(db_session, merchant_id, AS_OF)
        second = run_detection(db_session, merchant_id, AS_OF)

        assert first.risks_created == 1
        assert second.risks_created == 0
        assert second.risks_unchanged == 1

    def test_row_count_does_not_grow(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        for _ in range(5):
            run_detection(db_session, merchant_id, AS_OF)

        assert len(_stored(db_session, merchant_id)) == 1

    def test_risk_id_is_stable_across_runs(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)
        original_id = _stored(db_session, merchant_id)[0].id

        run_detection(db_session, merchant_id, AS_OF)
        assert _stored(db_session, merchant_id)[0].id == original_id

    def test_detected_at_is_not_moved_by_a_rerun(self, db_session: Session) -> None:
        """Moving it would erase how long the risk has been open."""
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)

        later = AS_OF + timedelta(days=1)
        run_detection(db_session, merchant_id, later)

        assert _stored(db_session, merchant_id)[0].detected_at == AS_OF

    @pytest.mark.parametrize("scenario", ["S04", "S05", "S06", "S10"])
    def test_idempotent_across_risk_types(self, db_session: Session, scenario: str) -> None:
        session = db_session
        merchant_id = _ingest(session, scenario)

        run_detection(session, merchant_id, AS_OF)
        run_detection(session, merchant_id, AS_OF)

        assert len(_stored(session, merchant_id)) == 1

    def test_subscription_rerun_is_idempotent(self, db_session: Session) -> None:
        """order_id IS NULL must match on lookup, or this duplicates."""
        merchant_id = _ingest(db_session, "S06")

        run_detection(db_session, merchant_id, AS_OF)
        run_detection(db_session, merchant_id, AS_OF)

        assert len(_stored(db_session, merchant_id)) == 1


class TestResolution:
    def test_late_success_resolves_the_stored_risk(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)
        assert _stored(db_session, merchant_id)[0].status == RiskStatus.DETECTED.value

        # The same order, now paid: S04's events plus a capture and order.paid.
        request = SimulationIngestRequest.model_validate(ingest_payload("S04"))
        order = request.entities.orders[0]
        late = SimulationIngestRequest.model_validate(
            {
                "entities": {
                    "merchants": [],
                    "customers": [],
                    "orders": [],
                    "payment_attempts": [],
                },
                "deliveries": [
                    {
                        "envelope": {"sequence": 1, "delivery_attempt": 1},
                        "event": {
                            "id": str(uuid.uuid4()),
                            "merchant_id": str(merchant_id),
                            "customer_id": str(order.customer_id),
                            "order_id": str(order.id),
                            "external_event_id": "late_capture_1",
                            "event_type": "payment.captured",
                            "payload": {
                                "order_ref": order.external_order_id,
                                "payment_ref": "sim_pay_42_9",
                                "amount_minor": order.amount,
                                "currency": "INR",
                                "attempt_number": 4,
                            },
                            "occurred_at": "2026-01-02T00:00:00Z",
                            "received_at": "2026-01-02T00:00:05Z",
                        },
                    },
                    {
                        "envelope": {"sequence": 2, "delivery_attempt": 1},
                        "event": {
                            "id": str(uuid.uuid4()),
                            "merchant_id": str(merchant_id),
                            "customer_id": str(order.customer_id),
                            "order_id": str(order.id),
                            "external_event_id": "late_paid_1",
                            "event_type": "order.paid",
                            "payload": {
                                "order_ref": order.external_order_id,
                                "amount_minor": order.amount,
                                "currency": "INR",
                            },
                            "occurred_at": "2026-01-02T00:00:10Z",
                            "received_at": "2026-01-02T00:00:12Z",
                        },
                    },
                ],
            }
        )
        ingest_simulation(db_session, late)
        db_session.flush()

        summary = run_detection(db_session, merchant_id, AS_OF)

        assert summary.risks_resolved == 1
        assert _stored(db_session, merchant_id)[0].status == RiskStatus.RECOVERED.value

    def test_resolution_reports_the_recovered_amount(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)

        request = SimulationIngestRequest.model_validate(ingest_payload("S04"))
        order = request.entities.orders[0]
        payload = {
            "entities": {"merchants": [], "customers": [], "orders": [], "payment_attempts": []},
            "deliveries": [
                {
                    "envelope": {"sequence": 1, "delivery_attempt": 1},
                    "event": {
                        "id": str(uuid.uuid4()),
                        "merchant_id": str(merchant_id),
                        "customer_id": str(order.customer_id),
                        "order_id": str(order.id),
                        "external_event_id": "late_capture_2",
                        "event_type": "payment.captured",
                        "payload": {
                            "order_ref": order.external_order_id,
                            "payment_ref": "sim_pay_42_9",
                            "amount_minor": order.amount,
                            "currency": "INR",
                            "attempt_number": 4,
                        },
                        "occurred_at": "2026-01-02T00:00:00Z",
                        "received_at": "2026-01-02T00:00:05Z",
                    },
                }
            ],
        }
        ingest_simulation(db_session, SimulationIngestRequest.model_validate(payload))
        db_session.flush()

        summary = run_detection(db_session, merchant_id, AS_OF)
        assert summary.total_recovered == order.amount

    def test_a_resolved_risk_is_not_reopened(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)

        risk = _stored(db_session, merchant_id)[0]
        risk.status = RiskStatus.RECOVERED.value
        db_session.flush()

        summary = run_detection(db_session, merchant_id, AS_OF)

        assert summary.risks_created == 0
        assert _stored(db_session, merchant_id)[0].status == RiskStatus.RECOVERED.value

    def test_expiry_is_persisted(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)

        summary = run_detection(db_session, merchant_id, AS_OF + timedelta(days=45))

        assert summary.risks_resolved == 1
        assert _stored(db_session, merchant_id)[0].status == RiskStatus.EXPIRED.value


class TestRunSummary:
    def test_counts_are_accurate(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        summary = run_detection(db_session, merchant_id, AS_OF)

        assert summary.merchant_id == merchant_id
        assert summary.as_of == AS_OF
        assert summary.orders_examined == 1
        assert summary.events_examined > 0
        assert summary.risks_touched == 1

    def test_total_at_risk_matches_open_rows(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        summary = run_detection(db_session, merchant_id, AS_OF)

        assert summary.total_amount_at_risk == _stored(db_session, merchant_id)[0].amount_at_risk

    def test_resolved_risk_leaves_the_at_risk_total(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)

        risk = _stored(db_session, merchant_id)[0]
        risk.status = RiskStatus.RECOVERED.value
        db_session.flush()

        summary = run_detection(db_session, merchant_id, AS_OF)
        assert summary.total_amount_at_risk == 0

    def test_naive_as_of_is_rejected(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        with pytest.raises(ValueError, match="timezone-aware"):
            run_detection(db_session, merchant_id, datetime(2026, 6, 1))

    def test_merchant_with_no_events_is_a_no_op(self, db_session: Session) -> None:
        summary = run_detection(db_session, uuid.uuid4(), AS_OF)
        assert summary.risks_touched == 0
        assert summary.events_examined == 0


class TestAuthorityBoundary:
    """Detection identifies risk. It writes nothing that authorises a response."""

    @pytest.mark.parametrize("scenario", ["S04", "S05", "S06", "S10", "S11", "S12"])
    def test_no_recovery_or_audit_rows(self, db_session: Session, scenario: str) -> None:
        session = db_session
        merchant_id = _ingest(session, scenario)
        run_detection(session, merchant_id, AS_OF)
        session.flush()

        for model in (RecoveryCase, RecoveryAction, AuditEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0

    def test_zero_recovery_rows_after_resolution(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)
        run_detection(db_session, merchant_id, AS_OF + timedelta(days=45))
        db_session.flush()

        for model in (RecoveryCase, RecoveryAction, AuditEvent):
            assert db_session.scalar(select(func.count()).select_from(model)) == 0

    def test_detection_service_imports_no_recovery_model(self) -> None:
        import ast
        import pathlib

        import app.services.detection.service as module

        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module == "app.models":
                names = {alias.name for alias in node.names}
                assert not names & {"RecoveryCase", "RecoveryAction", "AuditEvent"}

    def test_risk_repository_imports_no_recovery_model(self) -> None:
        import ast
        import pathlib

        import app.repositories.risk_repository as module

        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module == "app.models":
                names = {alias.name for alias in node.names}
                assert not names & {"RecoveryCase", "RecoveryAction", "AuditEvent"}


class TestRepositoryPrimitives:
    def test_natural_key_lookup_finds_the_row(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)
        stored = _stored(db_session, merchant_id)[0]

        found = risk_repository.find_by_natural_key(
            db_session, merchant_id, stored.order_id, stored.risk_type
        )
        assert found is not None and found.id == stored.id

    def test_natural_key_lookup_handles_null_order(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S06")
        run_detection(db_session, merchant_id, AS_OF)

        found = risk_repository.find_by_natural_key(
            db_session, merchant_id, None, RiskType.SUBSCRIPTION_PAYMENT_FAILURE.value
        )
        assert found is not None

    def test_unknown_key_returns_none(self, db_session: Session) -> None:
        assert (
            risk_repository.find_by_natural_key(
                db_session, uuid.uuid4(), None, RiskType.CHECKOUT_ABANDONMENT.value
            )
            is None
        )

    def test_only_open_filter(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)

        assert len(risk_repository.risks_for_merchant(db_session, merchant_id, only_open=True)) == 1

        _stored(db_session, merchant_id)[0].status = RiskStatus.RECOVERED.value
        db_session.flush()

        assert risk_repository.risks_for_merchant(db_session, merchant_id, only_open=True) == []

    def test_known_risks_hydrate_from_rows(self, db_session: Session) -> None:
        merchant_id = _ingest(db_session, "S04")
        run_detection(db_session, merchant_id, AS_OF)

        known = risk_repository.known_risks_for_merchant(db_session, merchant_id)
        assert len(known) == 1
        assert known[0].is_open
        assert known[0].detected_at == AS_OF
