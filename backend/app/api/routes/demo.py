"""The browser-facing demo endpoint. Offline, synthetic, always rolled back.

A judge should be able to watch the whole recovery path — failure, link,
webhook, verification, replay, refusal — without a terminal. This exposes the
run that `run_demo.py` already performs, and adds nothing to it.

**Deliberately not a `DbSession` route.** Every other endpoint takes the
application's request-scoped session, which is bound to `DATABASE_URL` — the
development database. A demo writes rows, and those rows must not be offered to
that database even inside a transaction that will be rolled back. The runner
therefore opens its own connection to `DEMO_DATABASE_URL` and closes it, so the
application's own engine is never involved.

**Off unless explicitly enabled.** `DEMO_DATABASE_URL` is empty by default and
the endpoint reports itself unavailable, so shipping this code does not ship a
running demo. `revtrace_dev` and `revtrace_hypothesis_test` are refused by name
whatever the setting holds.

**No HTTP equivalent of `--commit`.** The CLI can keep its rows; this cannot ask
to. There is no query parameter, no body, and no code path that commits — an
endpoint reachable from a browser without authentication should not be able to
write anything that survives.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import AppSettings
from app.integrations.razorpay.demo import PROVENANCE
from app.services.demo.runner import DemoUnavailable, execute, resolve_demo_dsn

router = APIRouter(prefix="/demo", tags=["demo"])


@router.get(
    "/status",
    summary="Whether the offline demo is available",
    description=(
        "Reports whether `DEMO_DATABASE_URL` names a database the demo may run "
        "against. When it does not, `reason` says why in the same words the run "
        "endpoint would use, so a UI can explain itself before offering a button "
        "that would fail.\n\n"
        "Never returns a DSN, a credential, or any part of one."
    ),
)
def demo_status(settings: AppSettings) -> dict[str, object]:
    try:
        resolve_demo_dsn(settings)
    except DemoUnavailable as exc:
        return {"enabled": False, "reason": str(exc), "provenance": PROVENANCE}
    return {"enabled": True, "reason": None, "provenance": PROVENANCE}


@router.post(
    "/run",
    summary="Run the offline synthetic demo",
    description=(
        "Walks the recovery path against the configured demo database and rolls "
        "the transaction back. Nothing is persisted, and there is no parameter "
        "that would change that.\n\n"
        "No Razorpay credential is read, no `razorpay.Client` is constructed, and "
        "no external connection is opened. The payment-link response comes from a "
        "deterministic offline demo provider implementing the adapter's interface; "
        "the signature verification, merchant derivation, idempotency and status "
        "transitions are the production code path.\n\n"
        "Returns 503 when the demo is not enabled or names a protected database."
    ),
)
def run_demo_endpoint(settings: AppSettings) -> dict[str, object]:
    try:
        run = execute(settings)
    except DemoUnavailable as exc:
        # The message names a setting and possibly a refused database name, and
        # never a DSN or a credential.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from None
    return run.as_dict()


__all__ = ["router"]
