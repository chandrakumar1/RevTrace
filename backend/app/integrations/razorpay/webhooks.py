"""Webhook signature verification. The endpoint's only authentication.

There is no other authentication on this route, and there cannot be: a webhook
is called by someone else's server, which holds no session and no API key. The
HMAC *is* the boundary, so everything here fails closed — a missing header, an
empty secret, a body that is not valid UTF-8 and a mismatched digest all refuse
identically, before a single byte of JSON is parsed.

**Raw bytes, not a parsed body.** Razorpay signs the exact octets it sent.
Re-serialising a parsed object produces different bytes — different key order,
different whitespace, different number formatting — and every signature would
fail for reasons that look like a configuration problem. The route reads
`await request.body()` and hands those bytes here untouched.

**The SDK does the comparison.** `razorpay.Client.utility.verify_webhook_signature`
is HMAC-SHA256 with `hmac.compare_digest`, which is what this needs, and using
the provider's own implementation means the algorithm cannot drift from theirs.
It takes a `str`, so the bytes are decoded first — lossless for valid UTF-8, and
a body that is not valid UTF-8 is refused rather than coerced.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings

#: The header Razorpay sends its signature in.
SIGNATURE_HEADER = "X-Razorpay-Signature"

#: The header naming the event, sent alongside the body. Advisory only — the
#: body is the authority, and this is never trusted for routing.
EVENT_HEADER = "X-Razorpay-Event-Id"


class WebhookVerificationError(Exception):
    """A webhook could not be authenticated. Nothing was read from its body.

    Deliberately not a subclass of `RazorpayError`: that hierarchy is about
    operations RevTrace performs, and this is about an inbound request RevTrace
    refuses. The message never contains the body, the signature, or the secret —
    a failed verification tells an attacker nothing beyond that it failed.
    """


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> None:
    """Refuse unless the body was signed with the shared secret.

    Returns None on success and raises on every failure, so a caller cannot
    accidentally treat a falsy return as a pass — the mistake that makes a
    boolean-returning verifier dangerous.
    """
    if not secret:
        raise WebhookVerificationError(
            "RAZORPAY_WEBHOOK_SECRET is not configured; the webhook endpoint "
            "cannot authenticate anything and refuses every request."
        )
    if not signature:
        raise WebhookVerificationError(f"missing {SIGNATURE_HEADER} header")
    if not raw_body:
        raise WebhookVerificationError("empty request body")

    try:
        body_text = raw_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Razorpay sends JSON, which is UTF-8 by definition. Anything else did
        # not come from Razorpay, and coercing it would mean signing something
        # other than what arrived.
        raise WebhookVerificationError("request body is not valid UTF-8") from exc

    import razorpay as razorpay_sdk
    from razorpay.errors import SignatureVerificationError

    utility = razorpay_sdk.Client(auth=("unused", "unused")).utility
    try:
        utility.verify_webhook_signature(body_text, signature, secret)
    except SignatureVerificationError as exc:
        # No detail: the comparison result is the only thing the caller learns.
        raise WebhookVerificationError("signature verification failed") from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise WebhookVerificationError("signature could not be verified") from exc


def verify_with_settings(raw_body: bytes, signature: str | None, settings: Settings) -> None:
    """`verify_signature` with the configured secret, read at call time.

    Read here rather than captured at import, so rotating the secret takes
    effect without a restart and a test can vary it without reloading a module.
    """
    verify_signature(raw_body, signature, settings.razorpay_webhook_secret.get_secret_value())


def sign(raw_body: bytes, secret: str) -> str:
    """The signature Razorpay would send for this body. **Test helper.**

    Lives beside the verifier on purpose: a test that hand-rolled its own HMAC
    would be asserting that two implementations agree, and would keep passing if
    both drifted the same way. This calls the same primitive the verifier does,
    so a test signs the way the provider does.
    """
    import hashlib
    import hmac

    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def event_name(payload: Any) -> str | None:
    """The `event` field, read defensively. The body is the authority.

    The `X-Razorpay-Event-Id` header is *not* used for this: a header is outside
    the signed bytes and can be changed by anyone in the path, while the body
    has just been proven authentic.
    """
    if not isinstance(payload, dict):
        return None
    name = payload.get("event")
    return name if isinstance(name, str) and name else None


__all__ = [
    "EVENT_HEADER",
    "SIGNATURE_HEADER",
    "WebhookVerificationError",
    "event_name",
    "sign",
    "verify_signature",
    "verify_with_settings",
]
