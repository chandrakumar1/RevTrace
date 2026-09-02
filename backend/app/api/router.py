"""Aggregate API router.

Feature routes live under `/api/v1`. Health stays at the root: liveness and
readiness probes are infrastructure concerns, not versioned product API, and
moving them would break anything already pointing at `/health`.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import demo, detection, health, ingestion, risks, timeline, webhooks

API_V1_PREFIX = "/api/v1"

#: Unversioned. Probes must not have to track an API version.
api_router = APIRouter()
api_router.include_router(health.router)

#: Versioned feature surface.
v1_router = APIRouter(prefix=API_V1_PREFIX)
v1_router.include_router(ingestion.router)
v1_router.include_router(detection.router)
v1_router.include_router(risks.router)
v1_router.include_router(timeline.router)
#: Authenticated by HMAC signature rather than by a dependency: the caller
#: is Razorpay's server, which holds no session and no API key.
v1_router.include_router(webhooks.router)
#: Off unless DEMO_DATABASE_URL is set, and never able to commit. Takes no
#: request-scoped session: the demo opens its own connection so the
#: application's database is never involved.
v1_router.include_router(demo.router)

api_router.include_router(v1_router)
