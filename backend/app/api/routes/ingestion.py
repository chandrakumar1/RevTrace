"""Simulation ingestion endpoint.

Thin: validation lives in the schema, persistence in the service. This module
only translates between HTTP and those two.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import DbSession
from app.schemas.ingestion import IngestionResponse, SimulationIngestRequest
from app.services.ingestion.errors import IngestionError
from app.services.ingestion.service import ingest_simulation

router = APIRouter(prefix="/ingest", tags=["ingestion"])


@router.post(
    "/simulation",
    response_model=IngestionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a simulator fixture",
    description=(
        "Accepts `{entities, deliveries}` — a simulator fixture with its answer "
        "key removed. A payload carrying `ground_truth` is rejected: it is "
        "evaluation data and must never reach detection input.\n\n"
        "Ingestion is idempotent. Duplicate deliveries are accepted, offered to "
        "the database, and suppressed by the UNIQUE(merchant_id, "
        "external_event_id) constraint; re-posting the same fixture is a no-op."
    ),
)
def ingest_simulation_fixture(
    payload: SimulationIngestRequest, session: DbSession
) -> IngestionResponse:
    try:
        result = ingest_simulation(session, payload)
    except IngestionError as exc:
        # A reference to an entity we have never seen: reject the batch rather
        # than write half a timeline.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return IngestionResponse(
        merchants_upserted=result.merchants_upserted,
        customers_upserted=result.customers_upserted,
        orders_upserted=result.orders_upserted,
        payment_attempts_upserted=result.payment_attempts_upserted,
        events_received=result.events_received,
        events_persisted=result.events_persisted,
        duplicates_suppressed=result.duplicates_suppressed,
        scenario_id=result.scenario_id,
        seed=result.seed,
    )
