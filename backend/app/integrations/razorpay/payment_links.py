"""Payment links, mapped down to the handful of fields RevTrace uses.

Thin on purpose. The adapter's job is to stop Razorpay's response shape from
spreading into services: everything past this module sees `PaymentLink`, which
has six fields and no provider vocabulary.

**No new domain model.** A payment link is the *result* of a
`create_payment_link` action, and `recovery_actions` already stores that —
`parameters` for what was asked, `result` for what came back, `idempotency_key`
for the guarantee that it happens once. `PaymentLink` is a transport shape
between this package and that row, not a table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.razorpay.client import RazorpayError

#: Razorpay expects the amount in the currency's smallest unit — paise for INR
#: — which is the same integer-minor-unit convention RevTrace uses everywhere.
#: No conversion is needed, and none is performed: a float would enter the money
#: path at exactly the point it must not.
CURRENCY = "INR"

#: Statuses a payment link can hold. Mirrors Razorpay's documented vocabulary
#: and is used only to recognise them, never to invent one.
LINK_CREATED = "created"
LINK_PAID = "paid"
LINK_PARTIALLY_PAID = "partially_paid"
LINK_EXPIRED = "expired"
LINK_CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({LINK_PAID, LINK_EXPIRED, LINK_CANCELLED})


class PaymentLinkError(RazorpayError):
    """A payment link could not be created or fetched."""


@dataclass(frozen=True, slots=True)
class PaymentLink:
    """What RevTrace needs from a Razorpay payment link, and nothing else.

    `provider_link_id` is the reconciliation anchor: it is what a later webhook
    names, and what `recovery_actions.result` stores so an execution can be
    matched to its outcome.

    Deliberately absent: the customer's contact details, the notes payload, the
    short URL's analytics, and every other field Razorpay returns. A field that
    is not mapped cannot be depended on, and a dependency on a provider's
    incidental field is how an adapter stops being an adapter.
    """

    provider_link_id: str
    status: str
    amount_minor: int
    currency: str
    short_url: str | None
    reference_id: str | None

    def __post_init__(self) -> None:
        if not self.provider_link_id:
            raise PaymentLinkError("a payment link with no provider id cannot be reconciled")
        if self.amount_minor < 0:
            raise PaymentLinkError(f"amount must not be negative, got {self.amount_minor}")

    @property
    def is_paid(self) -> bool:
        return self.status == LINK_PAID

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def as_result(self) -> dict[str, object]:
        """The JSONB shape stored in `recovery_actions.result`.

        Scalars only, so the audit snapshot guard accepts it and a reviewer can
        recompute what happened from stored values alone.
        """
        return {
            "provider": "razorpay",
            "provider_link_id": self.provider_link_id,
            "status": self.status,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "short_url": self.short_url,
            "reference_id": self.reference_id,
        }


def _require(payload: Any, field: str) -> Any:
    if not isinstance(payload, dict):
        raise PaymentLinkError(f"expected a payment-link object, got {type(payload).__name__}")
    if field not in payload:
        raise PaymentLinkError(f"payment-link response is missing {field!r}")
    return payload[field]


def to_payment_link(payload: Any) -> PaymentLink:
    """Razorpay's response to RevTrace's shape. Pure, so it is testable offline.

    Refuses rather than defaults: a missing `id`, `status` or `amount` means the
    response is not the thing it claims to be, and inventing a value would put a
    guess where a provider fact belongs.
    """
    amount = _require(payload, "amount")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise PaymentLinkError(f"amount must be an integer in minor units, got {amount!r}")

    return PaymentLink(
        provider_link_id=str(_require(payload, "id")),
        status=str(_require(payload, "status")),
        amount_minor=amount,
        currency=str(payload.get("currency") or CURRENCY),
        short_url=payload.get("short_url"),
        reference_id=payload.get("reference_id"),
    )


def build_request(
    *,
    amount_minor: int,
    reference_id: str,
    description: str,
    customer_name: str | None = None,
    customer_email: str | None = None,
    customer_contact: str | None = None,
    currency: str = CURRENCY,
) -> dict[str, object]:
    """The request body, built in one place so a test can assert its shape.

    `reference_id` is RevTrace's own identifier for the action. Razorpay stores
    it and echoes it back on the link and its webhooks, which is what lets a
    later out-of-order event be matched to the action that caused it without
    trusting delivery order.
    """
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise PaymentLinkError(f"amount must be an integer in minor units, got {amount_minor!r}")
    if amount_minor <= 0:
        raise PaymentLinkError(f"a payment link needs a positive amount, got {amount_minor}")
    if not reference_id:
        raise PaymentLinkError("reference_id is required so the link can be reconciled")

    customer: dict[str, str] = {}
    if customer_name:
        customer["name"] = customer_name
    if customer_email:
        customer["email"] = customer_email
    if customer_contact:
        customer["contact"] = customer_contact

    body: dict[str, object] = {
        "amount": amount_minor,
        "currency": currency,
        "description": description,
        "reference_id": reference_id,
        # RevTrace decides when and whether a customer is contacted; the policy
        # engine already counts contacts and enforces the cap. Letting the
        # provider also notify would put a second, uncounted channel outside
        # that budget.
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
    }
    if customer:
        body["customer"] = customer
    return body


def create_payment_link(client: Any, request: dict[str, object]) -> PaymentLink:
    """Create one link through the official SDK.

    The SDK's own exceptions are translated into `PaymentLinkError` so no
    provider type escapes this package — and the provider's message is not
    included, because it can quote the request that produced it.
    """
    import razorpay.errors as razorpay_errors

    try:
        response = client.payment_link.create(request)
    except razorpay_errors.BadRequestError as exc:
        raise PaymentLinkError(f"razorpay refused the payment link: {type(exc).__name__}") from exc
    except (razorpay_errors.ServerError, razorpay_errors.GatewayError) as exc:
        raise PaymentLinkError(f"razorpay is unavailable: {type(exc).__name__}") from exc
    return to_payment_link(response)


def fetch_payment_link(client: Any, provider_link_id: str) -> PaymentLink:
    """Read one link back. Used by verification, never to decide policy."""
    if not provider_link_id:
        raise PaymentLinkError("a payment link id is required")

    import razorpay.errors as razorpay_errors

    try:
        response = client.payment_link.fetch(provider_link_id)
    except razorpay_errors.BadRequestError as exc:
        raise PaymentLinkError(
            f"razorpay could not fetch the payment link: {type(exc).__name__}"
        ) from exc
    except (razorpay_errors.ServerError, razorpay_errors.GatewayError) as exc:
        raise PaymentLinkError(f"razorpay is unavailable: {type(exc).__name__}") from exc
    return to_payment_link(response)


__all__ = [
    "CURRENCY",
    "LINK_CANCELLED",
    "LINK_CREATED",
    "LINK_EXPIRED",
    "LINK_PAID",
    "LINK_PARTIALLY_PAID",
    "TERMINAL_STATUSES",
    "PaymentLink",
    "PaymentLinkError",
    "build_request",
    "create_payment_link",
    "fetch_payment_link",
    "to_payment_link",
]
