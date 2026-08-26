"""RevTrace backend application entrypoint.

Phase 1: application factory, logging, and health endpoints. The schema is
managed by Alembic — this module never calls `create_all()`, so the database
shape always comes from a reviewed migration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.app_env != "development")
    logger.info(
        "RevTrace backend starting",
        extra={
            "environment": settings.app_env,
            "razorpay_configured": settings.razorpay_configured,
            "gemini_configured": settings.gemini_configured,
        },
    )
    yield
    logger.info("RevTrace backend stopping")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="RevTrace",
        description=(
            "AI revenue recovery. Traces where revenue is leaking, determines why, "
            "recovers what is recoverable, and proves what happened. "
            "All revenue, risk, policy, and execution decisions are deterministic; "
            "the LLM is advisory only."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
    )
    app.include_router(api_router)
    return app


app = create_app()
