"""A deterministic, offline stand-in for Razorpay. **Synthetic, never real.**

RevTrace's buildathon demo has to show the whole recovery path — link created,
webhook received, event attributed, payment advanced — without credentials,
without a network, and without anyone's real money. This module provides that,
and is careful about one thing above all:

**Nothing here is a Razorpay transaction.** Every identifier carries a `DEMO`
marker, every response is labelled, and `PROVENANCE` travels with the data so a
screenshot of the output cannot be mistaken for a real capture. A demo that
looked real would be a false record, which is the one thing this project refuses
everywhere else.

**The adapter is exercised, not bypassed.** `DemoPaymentLinkClient` implements
the same `payment_link.create` / `payment_link.fetch` surface the official SDK
exposes, so `payment_links.create_payment_link` — the real mapping, the real
validation, the real error translation — runs unchanged. Swapping in this client
changes *where the bytes come from*, and nothing else.

**The security path is real.** The webhook fixtures below are signed with a
genuine HMAC-SHA256 over the exact bytes that will be delivered, using the same
`webhooks.sign` the verifier's own tests use. Verification, merchant derivation,
idempotency and ordering are all the production code. Only the secret is
synthetic, and it is a literal in this file rather than anything read from
configuration.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.integrations.razorpay.payment_links import (
    CURRENCY,
    LINK_CREATED,
    LINK_PAID,
    PaymentLinkError,
)

#: Stamped on every synthetic object this module produces.
PROVENANCE = "DEMO / SYNTHETIC / OFFLINE — not a Razorpay transaction"

#: A marker embedded in every identifier, so a demo id is recognisable at a
#: glance and can never be confused with a provider's own.
DEMO_MARKER = "DEMO"

#: The webhook secret the demo signs with. A literal, deliberately: reading a
#: real `RAZORPAY_WEBHOOK_SECRET` would make the demo depend on a credential it
#: has no need for, and this value authenticates nothing outside this process.
DEMO_WEBHOOK_SECRET = "demo-only-webhook-secret-not-a-real-credential"

#: Fixed instant, so two demo runs produce byte-identical output. A demo that
#: changed every time could not be diffed, and a reviewer could not tell a real
#: change from a clock tick.
DEMO_EPOCH = 1_767_225_600  # 2026-01-01T00:00:00Z


class DemoError(RuntimeError):
    """The demo could not build what it was asked for."""


def demo_id(kind: str, seed: str) -> str:
    """A deterministic identifier that announces itself as synthetic.

    Derived from the seed so a rerun reproduces it, and marked so nobody has to
    guess whether `plink_DEMO1a2b3c` came from Razorpay.
    """
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"revtrace.demo.{kind}.{seed}").hex[:12]
    return f"{kind}_{DEMO_MARKER}{digest}"


@dataclass(frozen=True, slots=True)
class DemoPaymentLinkClient:
    """Implements the SDK's payment-link surface, offline.

    Deliberately shaped like `razorpay.Client` rather than like a helper: the
    adapter under test takes a client and calls `client.payment_link.create`,
    so anything else would test a different code path than production uses.
    """

    reference_id: str
    status: str = LINK_CREATED

    @property
    def payment_link(self) -> DemoPaymentLinkResource:
        return DemoPaymentLinkResource(reference_id=self.reference_id, status=self.status)


@dataclass(frozen=True, slots=True)
class DemoPaymentLinkResource:
    """The `.payment_link` namespace: `create` and `fetch`, and nothing else."""

    reference_id: str
    status: str

    def _response(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        amount = int((request or {}).get("amount", 0))
        reference = str((request or {}).get("reference_id") or self.reference_id)
        link_id = demo_id("plink", reference)
        return {
            "id": link_id,
            "status": self.status,
            "amount": amount,
            "currency": str((request or {}).get("currency") or CURRENCY),
            "short_url": f"https://demo.invalid/{link_id}",
            "reference_id": reference,
            "created_at": DEMO_EPOCH,
            # Not a field the adapter maps; present so the demo response has the
            # same *shape* a real one does, and labelled so it cannot mislead.
            "description": PROVENANCE,
        }

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):  # pragma: no cover - defensive
            raise DemoError("the demo client expects the adapter's request body")
        return self._response(data)

    def fetch(self, payment_link_id: str) -> dict[str, Any]:
        if not payment_link_id:
            raise PaymentLinkError("a payment link id is required")
        response = self._response()
        response["id"] = payment_link_id
        return response


def paid_client(reference_id: str) -> DemoPaymentLinkClient:
    """A demo client whose link reads back as paid, for the recovery step."""
    return DemoPaymentLinkClient(reference_id=reference_id, status=LINK_PAID)


# -- synthetic webhooks ----------------------------------------------------


def webhook_body(
    event: str,
    *,
    payment_id: str,
    amount_minor: int = 50_000,
    occurred_at: int = DEMO_EPOCH,
    link_id: str | None = None,
) -> bytes:
    """One synthetic webhook, serialised exactly as it will be delivered.

    Returns **bytes**, not a dict, and the caller signs and delivers these same
    bytes. That is the whole point: the signature covers the octets, so a demo
    that handed a dict around and re-serialised it would verify something other
    than what it sent — the exact mistake the route is built to avoid.

    Compact separators, as a provider sends. `json.dumps` defaults would survive
    a parse/re-dump round trip unchanged and quietly weaken the demonstration.
    """
    entity: dict[str, Any] = {
        "id": payment_id,
        "amount": amount_minor,
        "currency": CURRENCY,
        "created_at": occurred_at,
        "description": PROVENANCE,
    }
    container = "payment"
    if link_id is not None:
        entity["payment_link_id"] = link_id
    payload = {
        "event": event,
        "created_at": occurred_at,
        "account_id": demo_id("acc", "revtrace"),
        "payload": {container: {"entity": entity}},
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def signed_webhook(
    event: str,
    *,
    payment_id: str,
    amount_minor: int = 50_000,
    occurred_at: int = DEMO_EPOCH,
    link_id: str | None = None,
    secret: str = DEMO_WEBHOOK_SECRET,
) -> tuple[bytes, str]:
    """The bytes and their real HMAC-SHA256 signature.

    The cryptography is genuine — `webhooks.sign` is the same primitive the
    verifier uses. Only the secret is synthetic.
    """
    from app.integrations.razorpay.webhooks import sign

    raw = webhook_body(
        event,
        payment_id=payment_id,
        amount_minor=amount_minor,
        occurred_at=occurred_at,
        link_id=link_id,
    )
    return raw, sign(raw, secret)


#: The three deliveries the demo walks through, in the order a recovery story
#: actually runs: a failure that creates the opportunity, a link, then the
#: capture that closes it. `payment_link.paid` is included because it maps to a
#: different `EventType` than its name suggests, which is worth showing.
DEMO_EVENTS: tuple[str, ...] = ("payment.failed", "payment.captured", "payment_link.paid")


def demo_scenario(payment_id: str, link_id: str) -> dict[str, tuple[bytes, str]]:
    """Every synthetic delivery for one payment, signed and ready.

    Timestamps advance with the story, so `occurred_at` ordering is
    demonstrable rather than incidental.
    """
    return {
        "payment.failed": signed_webhook(
            "payment.failed", payment_id=payment_id, occurred_at=DEMO_EPOCH
        ),
        "payment.captured": signed_webhook(
            "payment.captured", payment_id=payment_id, occurred_at=DEMO_EPOCH + 3_600
        ),
        "payment_link.paid": signed_webhook(
            "payment_link.paid",
            payment_id=payment_id,
            occurred_at=DEMO_EPOCH + 3_660,
            link_id=link_id,
        ),
    }


def as_label(payload: dict[str, object]) -> dict[str, object]:
    """Stamp any demo output so it cannot be read as a real provider response."""
    return {**payload, "provenance": PROVENANCE}


__all__ = [
    "DEMO_EPOCH",
    "DEMO_EVENTS",
    "DEMO_MARKER",
    "DEMO_WEBHOOK_SECRET",
    "PROVENANCE",
    "DemoError",
    "DemoPaymentLinkClient",
    "DemoPaymentLinkResource",
    "as_label",
    "demo_id",
    "demo_scenario",
    "paid_client",
    "signed_webhook",
    "webhook_body",
]
