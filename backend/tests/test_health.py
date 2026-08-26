"""Health endpoints.

Hermetic: the database connectivity probe is monkeypatched so no connection is
opened and nothing is written to revtrace_dev.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api.routes.health as health_module


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "revtrace-backend"
    assert "timestamp" in body


def test_health_db_reports_ok_when_connected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_module, "check_connection", lambda: True)

    body = client.get("/health/db").json()
    assert body["status"] == "ok"
    assert body["database_connected"] is True


def test_health_db_reports_degraded_when_disconnected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_module, "check_connection", lambda: False)

    response = client.get("/health/db")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "degraded"
    assert body["database_connected"] is False


def test_health_db_never_exposes_credential_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration flags report presence only."""
    monkeypatch.setattr(health_module, "check_connection", lambda: True)

    raw = client.get("/health/db").text
    body = client.get("/health/db").json()

    assert isinstance(body["razorpay_configured"], bool)
    assert isinstance(body["gemini_configured"], bool)
    for leaked in ("key_secret", "api_key", "rzp_test_", "password"):
        assert leaked not in raw


def test_openapi_schema_builds(client: TestClient) -> None:
    """A broken response model would surface here rather than at runtime."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/health" in response.json()["paths"]
