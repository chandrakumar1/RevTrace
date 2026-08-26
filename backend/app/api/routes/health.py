"""Health endpoints.

`/health` is a pure liveness probe and touches nothing. `/health/db` performs a
read-only `SELECT 1`; it never writes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.api.dependencies import AppSettings
from app.db.session import check_connection
from app.schemas.health import DbHealthResponse, HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
def health(settings: AppSettings) -> HealthResponse:
    return HealthResponse(
        environment=settings.app_env,
        timestamp=datetime.now(UTC),
    )


@router.get("/health/db", response_model=DbHealthResponse, summary="Database readiness")
def health_db(settings: AppSettings) -> DbHealthResponse:
    connected = check_connection()
    return DbHealthResponse(
        status="ok" if connected else "degraded",
        database_connected=connected,
        environment=settings.app_env,
        timestamp=datetime.now(UTC),
        razorpay_configured=settings.razorpay_configured,
        gemini_configured=settings.gemini_configured,
    )
