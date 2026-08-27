"""API tests against revtrace_test.

The app's `get_db` dependency is overridden with the rolled-back test session,
so every request runs inside the same transaction the fixture discards. Nothing
survives a test, and revtrace_dev is never involved.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.router import API_V1_PREFIX
from app.db.session import get_db
from app.main import create_app
from app.models import AuditEvent, RecoveryAction, RecoveryCase
from app.models.enums import RiskStatus, RiskType
from tests.ingestion.conftest import full_fixture, ingest_payload

pytestmark = pytest.mark.db

AS_OF = "2026-06-01T00:00:00Z"


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient]:
    """A client whose requests share the test's rolled-back session."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def _ingest(client: TestClient, scenario: str) -> dict:
    response = client.post(f"{API_V1_PREFIX}/ingest/simulation", json=ingest_payload(scenario))
    assert response.status_code == 201, response.text
    return response.json()


def _detect(client: TestClient, merchant_id: str, as_of: str = AS_OF) -> dict:
    response = client.post(
        f"{API_V1_PREFIX}/detection/runs", json={"merchant_id": merchant_id, "as_of": as_of}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _merchant_of(scenario: str) -> str:
    return ingest_payload(scenario)["entities"]["merchants"][0]["id"]


class TestRouting:
    def test_health_stays_at_the_root(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200

    def test_health_db_stays_at_the_root(self, client: TestClient) -> None:
        assert client.get("/health/db").status_code == 200

    def test_health_is_not_versioned(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/health").status_code == 404

    def test_feature_routes_are_versioned(self, client: TestClient) -> None:
        assert client.get("/risks").status_code == 404
        assert client.get(f"{API_V1_PREFIX}/risks").status_code == 200

    def test_openapi_lists_all_six_routes(self, client: TestClient) -> None:
        paths = set(client.get("/openapi.json").json()["paths"])
        assert {
            f"{API_V1_PREFIX}/ingest/simulation",
            f"{API_V1_PREFIX}/detection/runs",
            f"{API_V1_PREFIX}/risks",
            f"{API_V1_PREFIX}/risks/{{risk_id}}",
            f"{API_V1_PREFIX}/risks/{{risk_id}}/evidence",
            f"{API_V1_PREFIX}/orders/{{order_id}}/timeline",
        } <= paths

    def test_openapi_schema_builds(self, client: TestClient) -> None:
        assert client.get("/openapi.json").status_code == 200


class TestIngestionEndpoint:
    def test_accepts_a_fixture(self, client: TestClient) -> None:
        body = _ingest(client, "S04")
        assert body["events_persisted"] > 0
        assert body["duplicates_suppressed"] == 0
        assert body["scenario_id"] == "S04"

    def test_rejects_ground_truth(self, client: TestClient) -> None:
        """The API boundary refuses evaluation data outright."""
        response = client.post(f"{API_V1_PREFIX}/ingest/simulation", json=full_fixture("S04"))
        assert response.status_code == 422
        assert "ground_truth" in response.text

    def test_suppresses_duplicates(self, client: TestClient) -> None:
        body = _ingest(client, "S07")
        assert body["duplicates_suppressed"] > 0
        assert body["events_persisted"] < body["events_received"]

    def test_reingesting_is_a_no_op(self, client: TestClient) -> None:
        _ingest(client, "S04")
        second = _ingest(client, "S04")
        assert second["events_persisted"] == 0

    def test_unknown_merchant_reference_is_422(self, client: TestClient) -> None:
        payload = ingest_payload("S04")
        payload["entities"]["merchants"] = []
        response = client.post(f"{API_V1_PREFIX}/ingest/simulation", json=payload)

        assert response.status_code == 422
        assert "unknown merchants" in response.text

    def test_malformed_event_is_422(self, client: TestClient) -> None:
        payload = ingest_payload("S04")
        payload["deliveries"][0]["event"]["event_type"] = "payment.exploded"
        assert client.post(f"{API_V1_PREFIX}/ingest/simulation", json=payload).status_code == 422

    def test_float_money_is_422(self, client: TestClient) -> None:
        payload = ingest_payload("S04")
        payload["entities"]["orders"][0]["amount"] = 4999.0
        assert client.post(f"{API_V1_PREFIX}/ingest/simulation", json=payload).status_code == 422

    def test_empty_body_is_422(self, client: TestClient) -> None:
        assert client.post(f"{API_V1_PREFIX}/ingest/simulation", json={}).status_code == 422


class TestDetectionEndpoint:
    def test_runs_and_creates_a_risk(self, client: TestClient) -> None:
        merchant_id = _ingest(client, "S04")["scenario_id"] and _merchant_of("S04")
        body = _detect(client, merchant_id)

        assert body["risks_created"] == 1
        assert body["total_amount_at_risk_minor"] > 0
        assert body["orders_examined"] == 1

    def test_clean_scenario_creates_nothing(self, client: TestClient) -> None:
        _ingest(client, "S01")
        body = _detect(client, _merchant_of("S01"))
        assert body["risks_created"] == 0
        assert body["total_amount_at_risk_minor"] == 0

    def test_rerun_is_idempotent(self, client: TestClient) -> None:
        _ingest(client, "S04")
        merchant_id = _merchant_of("S04")

        first = _detect(client, merchant_id)
        second = _detect(client, merchant_id)

        assert first["risks_created"] == 1
        assert second["risks_created"] == 0
        assert second["risks_unchanged"] == 1

    def test_as_of_is_required(self, client: TestClient) -> None:
        response = client.post(
            f"{API_V1_PREFIX}/detection/runs", json={"merchant_id": str(uuid.uuid4())}
        )
        assert response.status_code == 422

    def test_naive_as_of_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            f"{API_V1_PREFIX}/detection/runs",
            json={"merchant_id": str(uuid.uuid4()), "as_of": "2026-06-01T00:00:00"},
        )
        assert response.status_code == 422
        assert "timezone-aware" in response.text

    def test_unknown_field_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            f"{API_V1_PREFIX}/detection/runs",
            json={"merchant_id": str(uuid.uuid4()), "as_of": AS_OF, "force": True},
        )
        assert response.status_code == 422

    def test_unknown_merchant_is_an_empty_run(self, client: TestClient) -> None:
        body = _detect(client, str(uuid.uuid4()))
        assert body["events_examined"] == 0
        assert body["risks_created"] == 0

    def test_resolution_is_reported(self, client: TestClient) -> None:
        _ingest(client, "S04")
        merchant_id = _merchant_of("S04")
        _detect(client, merchant_id)

        later = (datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=45)).isoformat()
        body = _detect(client, merchant_id, later)

        assert body["risks_resolved"] == 1
        assert body["resolutions"][0]["new_status"] == RiskStatus.EXPIRED.value
        assert body["resolutions"][0]["reason"]


class TestRiskListEndpoint:
    def test_lists_a_detected_risk(self, client: TestClient) -> None:
        _ingest(client, "S04")
        _detect(client, _merchant_of("S04"))

        body = client.get(f"{API_V1_PREFIX}/risks").json()
        assert body["total"] == 1
        assert body["items"][0]["risk_type"] == RiskType.REPEATED_PAYMENT_FAILURE.value

    def test_empty_when_nothing_detected(self, client: TestClient) -> None:
        body = client.get(f"{API_V1_PREFIX}/risks").json()
        assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}

    def test_filters_by_merchant(self, client: TestClient) -> None:
        _ingest(client, "S04")
        _detect(client, _merchant_of("S04"))

        body = client.get(
            f"{API_V1_PREFIX}/risks", params={"merchant_id": str(uuid.uuid4())}
        ).json()
        assert body["total"] == 0

    def test_filters_by_risk_type(self, client: TestClient) -> None:
        _ingest(client, "S04")
        _detect(client, _merchant_of("S04"))

        hit = client.get(
            f"{API_V1_PREFIX}/risks", params={"risk_type": "repeated_payment_failure"}
        ).json()
        miss = client.get(
            f"{API_V1_PREFIX}/risks", params={"risk_type": "checkout_abandonment"}
        ).json()

        assert hit["total"] == 1
        assert miss["total"] == 0

    def test_filters_by_status(self, client: TestClient) -> None:
        _ingest(client, "S04")
        _detect(client, _merchant_of("S04"))

        body = client.get(f"{API_V1_PREFIX}/risks", params={"status": "detected"}).json()
        assert body["total"] == 1

    def test_unknown_risk_type_is_422(self, client: TestClient) -> None:
        response = client.get(f"{API_V1_PREFIX}/risks", params={"risk_type": "nope"})
        assert response.status_code == 422

    def test_unknown_status_is_422(self, client: TestClient) -> None:
        response = client.get(f"{API_V1_PREFIX}/risks", params={"status": "nope"})
        assert response.status_code == 422

    def test_limit_is_bounded(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/risks", params={"limit": 0}).status_code == 422
        assert client.get(f"{API_V1_PREFIX}/risks", params={"limit": 500}).status_code == 422

    def test_negative_offset_is_422(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/risks", params={"offset": -1}).status_code == 422

    def test_pagination_echoes_the_window(self, client: TestClient) -> None:
        body = client.get(f"{API_V1_PREFIX}/risks", params={"limit": 5, "offset": 2}).json()
        assert body["limit"] == 5
        assert body["offset"] == 2

    def test_order_ref_is_resolved(self, client: TestClient) -> None:
        _ingest(client, "S04")
        _detect(client, _merchant_of("S04"))

        item = client.get(f"{API_V1_PREFIX}/risks").json()["items"][0]
        assert item["order_ref"] == "sim_order_42_1"


class TestRiskDetailEndpoint:
    def _risk_id(self, client: TestClient, scenario: str = "S04") -> str:
        _ingest(client, scenario)
        _detect(client, _merchant_of(scenario))
        return client.get(f"{API_V1_PREFIX}/risks").json()["items"][0]["risk_id"]

    def test_returns_the_risk(self, client: TestClient) -> None:
        risk_id = self._risk_id(client)
        body = client.get(f"{API_V1_PREFIX}/risks/{risk_id}").json()

        assert body["risk_id"] == risk_id
        assert body["status"] == RiskStatus.DETECTED.value
        assert body["evidence_url"].endswith(f"/risks/{risk_id}/evidence")

    def test_is_true_positive_is_null(self, client: TestClient) -> None:
        risk_id = self._risk_id(client)
        assert client.get(f"{API_V1_PREFIX}/risks/{risk_id}").json()["is_true_positive"] is None

    def test_unknown_risk_is_404(self, client: TestClient) -> None:
        response = client.get(f"{API_V1_PREFIX}/risks/{uuid.uuid4()}")
        assert response.status_code == 404
        assert "not found" in response.text

    def test_malformed_uuid_is_422(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/risks/not-a-uuid").status_code == 422


class TestRiskEvidenceEndpoint:
    def _risk_id(self, client: TestClient, scenario: str) -> str:
        _ingest(client, scenario)
        _detect(client, _merchant_of(scenario))
        return client.get(f"{API_V1_PREFIX}/risks").json()["items"][0]["risk_id"]

    def test_derives_evidence_from_events(self, client: TestClient) -> None:
        risk_id = self._risk_id(client, "S04")
        body = client.get(f"{API_V1_PREFIX}/risks/{risk_id}/evidence").json()

        assert body["contributing_event_ids"]
        assert body["events_examined"] > 0
        assert body["order_state"] == "attempted"

    def test_reports_the_attempt_ledger(self, client: TestClient) -> None:
        risk_id = self._risk_id(client, "S04")
        body = client.get(f"{API_V1_PREFIX}/risks/{risk_id}/evidence").json()

        assert len(body["attempts"]) == 3
        assert all(a["failure_code"] == "card_declined" for a in body["attempts"])

    def test_reports_money_in_minor_units(self, client: TestClient) -> None:
        risk_id = self._risk_id(client, "S04")
        money = client.get(f"{API_V1_PREFIX}/risks/{risk_id}/evidence").json()["money"]

        assert isinstance(money["order_amount_minor"], int)
        assert money["captured_minor"] == 0
        assert money["outstanding_minor"] == money["order_amount_minor"]

    def test_reports_integrity_flags(self, client: TestClient) -> None:
        risk_id = self._risk_id(client, "S12b")
        integrity = client.get(f"{API_V1_PREFIX}/risks/{risk_id}/evidence").json()["integrity"]
        assert integrity["inferred_gaps"] == 1

    def test_inferred_attempt_is_flagged(self, client: TestClient) -> None:
        risk_id = self._risk_id(client, "S12b")
        attempts = client.get(f"{API_V1_PREFIX}/risks/{risk_id}/evidence").json()["attempts"]
        assert sum(1 for a in attempts if a["inferred"]) == 1

    def test_current_reason_is_derived(self, client: TestClient) -> None:
        risk_id = self._risk_id(client, "S04")
        body = client.get(f"{API_V1_PREFIX}/risks/{risk_id}/evidence").json()
        assert "failed payment attempts" in body["current_reason"]

    def test_subscription_evidence_has_no_order(self, client: TestClient) -> None:
        risk_id = self._risk_id(client, "S06")
        body = client.get(f"{API_V1_PREFIX}/risks/{risk_id}/evidence").json()

        assert body["order_id"] is None
        assert body["contributing_event_ids"]

    def test_unknown_risk_is_404(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/risks/{uuid.uuid4()}/evidence").status_code == 404


class TestTimelineEndpoint:
    def _order_id(self, client: TestClient, scenario: str) -> str:
        _ingest(client, scenario)
        return ingest_payload(scenario)["entities"]["orders"][0]["id"]

    def test_returns_the_causal_timeline(self, client: TestClient) -> None:
        order_id = self._order_id(client, "S04")
        body = client.get(f"{API_V1_PREFIX}/orders/{order_id}/timeline").json()

        positions = [e["causal_position"] for e in body["entries"]]
        assert positions == list(range(1, len(positions) + 1))
        assert body["state"] == "attempted"

    def test_delivery_position_is_reported(self, client: TestClient) -> None:
        order_id = self._order_id(client, "S04")
        body = client.get(f"{API_V1_PREFIX}/orders/{order_id}/timeline").json()
        assert all(e["delivery_position"] is not None for e in body["entries"])

    def test_delay_survives_to_the_api(self, client: TestClient) -> None:
        order_id = self._order_id(client, "S09")
        body = client.get(f"{API_V1_PREFIX}/orders/{order_id}/timeline").json()
        assert max(e["delay_seconds"] for e in body["entries"]) >= 6 * 60 * 60

    def test_entries_carry_a_summary(self, client: TestClient) -> None:
        order_id = self._order_id(client, "S04")
        body = client.get(f"{API_V1_PREFIX}/orders/{order_id}/timeline").json()
        assert all(e["summary"] for e in body["entries"])
        assert any("card_declined" in e["summary"] for e in body["entries"])

    def test_paid_order_reports_terminal_success(self, client: TestClient) -> None:
        order_id = self._order_id(client, "S01")
        body = client.get(f"{API_V1_PREFIX}/orders/{order_id}/timeline").json()

        assert body["reached_terminal_success"] is True
        assert body["has_order_paid"] is True

    def test_unreconciled_order_is_distinguishable(self, client: TestClient) -> None:
        order_id = self._order_id(client, "S10")
        body = client.get(f"{API_V1_PREFIX}/orders/{order_id}/timeline").json()

        assert body["has_capture"] is True
        assert body["has_order_paid"] is False

    def test_unknown_order_is_404(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/orders/{uuid.uuid4()}/timeline").status_code == 404

    def test_malformed_uuid_is_422(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/orders/nope/timeline").status_code == 422


class TestResponseSchemas:
    def test_confidence_is_flagged_as_a_heuristic(self, client: TestClient) -> None:
        """It must never be renderable as a calibrated probability."""
        _ingest(client, "S04")
        _detect(client, _merchant_of("S04"))

        item = client.get(f"{API_V1_PREFIX}/risks").json()["items"][0]
        assert item["confidence_is_synthetic_heuristic"] is True
        assert 0 <= item["confidence_bps"] <= 10_000

    def test_money_fields_are_integers(self, client: TestClient) -> None:
        _ingest(client, "S04")
        _detect(client, _merchant_of("S04"))

        item = client.get(f"{API_V1_PREFIX}/risks").json()["items"][0]
        assert isinstance(item["amount_at_risk_minor"], int)

    def test_reconciliation_risk_reports_zero_at_risk(self, client: TestClient) -> None:
        _ingest(client, "S10")
        _detect(client, _merchant_of("S10"))

        item = client.get(f"{API_V1_PREFIX}/risks").json()["items"][0]
        assert item["risk_type"] == RiskType.RECONCILIATION_MISMATCH.value
        assert item["amount_at_risk_minor"] == 0

    def test_timestamps_are_iso_utc(self, client: TestClient) -> None:
        _ingest(client, "S04")
        _detect(client, _merchant_of("S04"))

        detected_at = client.get(f"{API_V1_PREFIX}/risks").json()["items"][0]["detected_at"]
        assert datetime.fromisoformat(detected_at).utcoffset() == timedelta(0)


class TestSecurityPosture:
    """Phase 3 has no authentication. These tests pin the actual state.

    Adding real auth is a separate architectural decision, not something to
    invent inside a detection milestone. What is enforced here is the property
    that does exist: no secret ever crosses the wire.
    """

    def test_endpoints_are_currently_unauthenticated(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/risks").status_code == 200

    def test_openapi_declares_no_security_scheme(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()
        assert "securitySchemes" not in spec.get("components", {})

    def test_no_secret_leaks_in_health(self, client: TestClient) -> None:
        body = client.get("/health/db").text
        for marker in ("key_secret", "api_key", "rzp_test_", "password", "DATABASE_URL"):
            assert marker not in body

    def test_health_db_reports_presence_only(self, client: TestClient) -> None:
        body = client.get("/health/db").json()
        assert body["razorpay_configured"] is False
        assert body["gemini_configured"] is False

    def test_no_secret_leaks_in_risk_responses(self, client: TestClient) -> None:
        _ingest(client, "S04")
        _detect(client, _merchant_of("S04"))

        body = client.get(f"{API_V1_PREFIX}/risks").text
        for marker in ("key_secret", "api_key", "password", "postgresql"):
            assert marker not in body

    def test_error_bodies_do_not_leak_a_dsn(self, client: TestClient) -> None:
        response = client.get(f"{API_V1_PREFIX}/risks/{uuid.uuid4()}")
        assert "postgresql" not in response.text
        assert "sancha" not in response.text


class TestAuthorityBoundary:
    def test_api_never_creates_recovery_or_audit_rows(
        self, client: TestClient, db_session: Session
    ) -> None:
        for scenario in ("S04", "S06", "S10"):
            _ingest(client, scenario)
            _detect(client, _merchant_of(scenario))
        db_session.flush()

        for model in (RecoveryCase, RecoveryAction, AuditEvent):
            assert db_session.scalar(select(func.count()).select_from(model)) == 0

    def test_no_endpoint_approves_or_executes_anything(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        for path in paths:
            for word in ("approve", "execute", "recovery", "policy"):
                assert word not in path
