"""The Razorpay webhook endpoint. The signature is its only authentication.

This is the first route in RevTrace that a stranger can call, and it is called
by someone else's server — which holds no session and no API key. There is
therefore nothing to authenticate *with* except the shared secret, so the HMAC
check is not a validation step before the real work: it **is** the security
boundary, and it runs before the body is parsed.

**Raw bytes, deliberately.** `await request.body()` is read and verified before
anything looks at the JSON. Razorpay signs the exact octets it sent, and a
parsed-then-re-serialised body is different octets — different key order,
different spacing — so a route that verified a re-serialisation would reject
every genuine webhook and, worse, could be made to accept a forged one if the
comparison were ever loosened to compensate.

**Nothing about the body is logged.** Not on success, not on failure. A webhook
body carries payment identifiers and customer contact details, and an endpoint
that logs what it rejects is a way to make it log anything.

**No execution endpoint accompanies this.** A route that created a payment link
would be an unauthenticated money-moving primitive; execution stays behind the
policy gate and an operator, exactly as `run_hypothesis.py` did for the model.

**`merchant_id` is an assertion, not a selection.** It is optional, it lives in
the query string — outside the signed bytes — and the service derives the real
owner from the signed payment id instead. Supplying it can only cause a
rejection. It was authoritative once, and that was a cross-tenant defect: a
validly-signed webhook about one merchant's payment could be filed under
another, pointing at the victim's order and advancing the victim's payment
attempt.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.api.dependencies import AppSettings, DbSession
from app.integrations.razorpay.webhooks import (
    EVENT_HEADER,
    SIGNATURE_HEADER,
    WebhookVerificationError,
    verify_with_settings,
)
from app.services.verification.service import VerificationError, apply_webhook

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: Bodies above this are refused unread. Razorpay's webhooks are a few
#: kilobytes; anything far larger is not one, and reading it to find out is the
#: cheapest denial-of-service an unauthenticated endpoint can offer.
MAX_BODY_BYTES = 512 * 1024


@router.post(
    "/razorpay",
    status_code=status.HTTP_200_OK,
    summary="Receive a Razorpay webhook",
    description=(
        "Verifies `X-Razorpay-Signature` (HMAC-SHA256) against the exact raw "
        "request body before parsing it. A missing, malformed or mismatched "
        "signature is refused with 401 and nothing is read from the body.\n\n"
        "Delivery is idempotent: a repeated `external_event_id` is declined by "
        "`UNIQUE(merchant_id, external_event_id)` and reported as a duplicate. "
        "Ordering is taken from the provider's own timestamp, so delayed and "
        "out-of-order deliveries land correctly.\n\n"
        "An event RevTrace does not map is acknowledged with 200 and not "
        "stored — refusing it would only make Razorpay redeliver it.\n\n"
        "`merchant_id` is optional and only an assertion: the owning merchant is "
        "derived from the signed payment id, and a mismatch is rejected."
    ),
)
async def receive_razorpay_webhook(
    request: Request,
    session: DbSession,
    settings: AppSettings,
    merchant_id: uuid.UUID | None = None,
    signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
    event_id: Annotated[str | None, Header(alias=EVENT_HEADER)] = None,
) -> dict[str, object]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="webhook body too large",
        )

    # The boundary. Everything below may trust these bytes; nothing above may.
    try:
        verify_with_settings(raw, signature, settings)
    except WebhookVerificationError:
        # Deliberately uniform: a caller learns that verification failed and
        # never which check failed. Distinguishing "no secret configured" from
        # "wrong signature" would tell an attacker about our deployment.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="signature verification failed",
        ) from None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="body is not valid JSON",
        ) from None
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="body is not a JSON object",
        )

    try:
        outcome = apply_webhook(
            session,
            payload,
            received_at=datetime.now(UTC),
            asserted_merchant_id=merchant_id,
            header_event_id=event_id,
        )
    except VerificationError as exc:
        # Our own refusal, not the provider's fault. The message names what was
        # wrong with the request shape and never quotes the body.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    session.commit()
    return outcome.as_dict()


__all__ = ["MAX_BODY_BYTES", "router"]
