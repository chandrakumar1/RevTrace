"""Detection run endpoint.

Triggers the M7 service. No detection logic lives here.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.dependencies import DbSession
from app.schemas.detection import DetectionRunRequest, DetectionRunResponse
from app.services.detection.service import run_detection

router = APIRouter(prefix="/detection", tags=["detection"])


@router.post(
    "/runs",
    response_model=DetectionRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Run deterministic detection for one merchant",
    description=(
        "Reconstructs every timeline for the merchant, runs the deterministic "
        "detectors, and reconciles the results against previously stored risks.\n\n"
        "`as_of` is required and has no default — a run that read the server "
        "clock would not be reproducible. Re-running with the same `as_of` is "
        "idempotent: risks are upserted on (merchant_id, order_id, risk_type).\n\n"
        "Detection writes to `revenue_risks` only. It never creates a recovery "
        "case, a recovery action, or an audit entry: identifying a risk is not "
        "authorising a response to it."
    ),
)
def create_detection_run(payload: DetectionRunRequest, session: DbSession) -> DetectionRunResponse:
    summary = run_detection(session, payload.merchant_id, payload.as_of)
    return DetectionRunResponse.from_summary(summary)
