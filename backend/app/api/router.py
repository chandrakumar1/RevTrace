"""Aggregate API router.

Feature routers are mounted here as their phases land. Health is the only
router in Phase 1.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import health

api_router = APIRouter()
api_router.include_router(health.router)
