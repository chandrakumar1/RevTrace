"""RevTrace backend application entrypoint.

Phase 1: application factory, logging, and health endpoints. The schema is
managed by Alembic — this module never calls `create_all()`, so the database
shape always comes from a reviewed migration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

#: The only methods the browser client uses: `GET /api/v1/demo/status` and
#: `POST /api/v1/demo/run`. Nothing else is reachable cross-origin, so a future
#: endpoint has to be permitted deliberately rather than inherit access.
CORS_METHODS = ["GET", "POST"]

#: The client sends no custom header today. `Content-Type` is allowed so that a
#: request that later carries a JSON body does not fail its preflight for a
#: reason nobody would guess; every other header stays refused.
CORS_HEADERS = ["Content-Type"]


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
    _configure_cors(app, settings)
    app.include_router(api_router)
    return app


def _configure_cors(app: FastAPI, settings: Settings) -> None:
    """Permit exactly one browser origin, and only when one is configured.

    **No middleware at all when `FRONTEND_ORIGIN` is empty**, which is the local
    development case: the Vite dev server proxies `/api`, so the browser sees a
    single origin and there is no cross-origin request to permit. Installing
    permissive middleware to cover a case that does not arise is how a wildcard
    ends up in production.

    Three deliberate narrowings when it *is* configured:

    **One origin, never a wildcard.** `allow_origins=["*"]` would let any page
    on the internet call this API from a visitor's browser.

    **No credentials.** The API has no cookie, session or `Authorization`
    header, so there is nothing for a browser to attach. Enabling credentials
    would ask browsers to send whatever might exist later, which is the
    combination that turns a CORS relaxation into a real vulnerability.

    **Only the methods and headers the client actually uses.** A new endpoint
    should have to be permitted on purpose.
    """
    if not settings.cors_enabled:
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=False,
        allow_methods=CORS_METHODS,
        allow_headers=CORS_HEADERS,
    )


app = create_app()
