"""The seam where a verified webhook becomes a row.

Reached only after `integrations.razorpay.webhooks.verify_signature` has passed,
so everything here may trust that the bytes came from Razorpay. Nothing here
re-authenticates, and nothing here parses a body — it receives an already-parsed
payload whose signature was checked against the raw octets.

Four properties, each carried by something that already exists rather than by
code written here:

**Idempotent.** `UNIQUE(merchant_id, external_event_id)` and
`event_repository.insert_events`, which offers rows to the database and lets the
constraint decline the repeats. A duplicate delivery is therefore not a special
case to detect — it is an insert that writes nothing.

**Order-independent.** `occurred_at` comes from the provider's own timestamp and
`received_at` from the clock; timelines are reconstructed by the former. A
webhook that arrives late, or before the one that logically precedes it, lands
in the right place because nothing reads insertion order.

**Never an AI execution.** This writes `payment_attempts` and `events` and
**nothing else** — it writes no `audit_events` row at all, and does not touch
`recovery_actions.executed`. So there is no actor for the AI to become on this
path. The database refuses an `ai_agent` actor on an execution entry
independently, so the rule holds even if this module is wrong.

**State-machine safe.** A payment attempt moves forward only. A `captured`
attempt is not returned to `authorized` by a webhook that arrives afterwards,
which is exactly what out-of-order delivery would otherwise cause.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.razorpay.mapper import UnmappedEvent, is_ignored, map_event
from app.models.enums import EventType, PaymentStatus
from app.models.merchant import Merchant
from app.models.order import Order
from app.models.payment_attempt import PaymentAttempt
from app.repositories import event_repository

#: How far a payment attempt may advance. A webhook may move an attempt to the
#: right of its current position and never to the left, so a delayed
#: `payment.authorized` cannot undo a `payment.captured` that already arrived.
#:
#: `refunded` is terminal and deliberately reachable from `captured` only: a
#: refund of something never captured is not a state transition, it is a
#: contradiction, and the ingest should record the event without rewriting the
#: attempt.
STATUS_RANK: dict[str, int] = {
    PaymentStatus.CREATED.value: 0,
    PaymentStatus.TIMEOUT.value: 1,
    PaymentStatus.FAILED.value: 1,
    PaymentStatus.AUTHORIZED.value: 2,
    PaymentStatus.CAPTURED.value: 3,
    PaymentStatus.REFUNDED.value: 4,
}

#: Which RevTrace event implies which payment status. Only these three change an
#: attempt; the rest are recorded as timeline facts and nothing else.
STATUS_FROM_EVENT: dict[EventType, PaymentStatus] = {
    EventType.PAYMENT_AUTHORIZED: PaymentStatus.AUTHORIZED,
    EventType.PAYMENT_CAPTURED: PaymentStatus.CAPTURED,
    EventType.PAYMENT_FAILED: PaymentStatus.FAILED,
}


class VerificationError(ValueError):
    """A verified webhook could not be applied. Nothing was written."""


@dataclass(frozen=True, slots=True)
class WebhookOutcome:
    """What one delivery did. Every field is a count or a name, never a payload."""

    event_name: str
    external_event_id: str | None
    event_type: str | None
    persisted: bool
    duplicate: bool
    ignored: bool
    unmapped: bool
    payment_attempt_updated: bool
    previous_status: str | None = None
    new_status: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "event_name": self.event_name,
            "external_event_id": self.external_event_id,
            "event_type": self.event_type,
            "persisted": self.persisted,
            "duplicate": self.duplicate,
            "ignored": self.ignored,
            "unmapped": self.unmapped,
            "payment_attempt_updated": self.payment_attempt_updated,
            "previous_status": self.previous_status,
            "new_status": self.new_status,
        }


def _epoch_to_utc(value: Any) -> datetime | None:
    """Razorpay sends `created_at` as a Unix epoch second."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return None


def occurred_at_of(payload: dict[str, Any], *, received_at: datetime) -> datetime:
    """When the event happened, per the provider.

    Falls back to `received_at` only when the payload carries no usable
    timestamp. That fallback is explicitly *not* silent ordering by arrival: it
    is the best available statement about when the thing happened, and it is
    used identically whether the delivery was prompt or late.
    """
    top = _epoch_to_utc(payload.get("created_at"))
    if top is not None:
        return top
    entity = _first_entity(payload)
    if entity is not None:
        nested = _epoch_to_utc(entity.get("created_at"))
        if nested is not None:
            return nested
    return received_at


def _first_entity(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The first entity Razorpay wrapped in `payload.<kind>.entity`."""
    container = payload.get("payload")
    if not isinstance(container, dict):
        return None
    for value in container.values():
        if isinstance(value, dict):
            entity = value.get("entity")
            if isinstance(entity, dict):
                return entity
    return None


def external_event_id_of(payload: dict[str, Any], header_id: str | None) -> str | None:
    """The idempotency key for this delivery.

    Prefers the provider's own event id from the signed body. The header is used
    only as a fallback, and it is worth naming why that is acceptable here while
    the *event name* is read from the body alone: an attacker who alters this
    value can at worst cause a duplicate to be stored or a genuine event to be
    suppressed for themselves — they cannot change what the event means. A
    forged event *name* would change the leak graph, so that one is never taken
    from a header.
    """
    entity = _first_entity(payload)
    if entity is not None:
        entity_id = entity.get("id")
        if isinstance(entity_id, str) and entity_id:
            name = payload.get("event")
            return f"{name}:{entity_id}" if isinstance(name, str) else entity_id
    return header_id or None


def _payment_attempt_for(session: Session, payload: dict[str, Any]) -> PaymentAttempt | None:
    """The attempt this event is about, matched on the provider's payment id.

    The payment id comes from the **signed** body, so the row this returns is
    reached from data the HMAC covered.
    """
    entity = _first_entity(payload)
    if entity is None:
        return None
    payment_id = entity.get("id")
    if not isinstance(payment_id, str) or not payment_id:
        return None
    return session.execute(
        select(PaymentAttempt).where(PaymentAttempt.external_payment_id == payment_id)
    ).scalar_one_or_none()


def resolve_merchant(session: Session, attempt: PaymentAttempt) -> uuid.UUID:
    """The merchant that owns this payment, via its order.

    The authoritative tenant. Reached from the signed payment id through
    `payment_attempts -> orders -> merchant_id`, so tenancy is a function of
    data Razorpay signed rather than of anything the caller chose.
    """
    merchant_id = session.execute(
        select(Order.merchant_id).where(Order.id == attempt.order_id)
    ).scalar_one_or_none()
    if merchant_id is None:  # pragma: no cover - FK makes this unreachable
        raise VerificationError(
            f"payment attempt {attempt.id} has no order, so its merchant cannot be established"
        )
    return merchant_id


def advance_status(current: str, proposed: str) -> str:
    """Move an attempt forward, never back.

    Out-of-order delivery is the reason this exists: Razorpay does not promise
    ordering, so `payment.authorized` can arrive after `payment.captured`.
    Applying it blindly would rewrite a captured payment as merely authorized
    and make the recovery ledger disagree with the money.

    An unknown status is treated as unranked and refused rather than assumed to
    be newer.
    """
    current_rank = STATUS_RANK.get(current)
    proposed_rank = STATUS_RANK.get(proposed)
    if current_rank is None or proposed_rank is None:
        return current
    return proposed if proposed_rank > current_rank else current


def apply_webhook(
    session: Session,
    payload: dict[str, Any],
    *,
    received_at: datetime,
    asserted_merchant_id: uuid.UUID | None = None,
    header_event_id: str | None = None,
) -> WebhookOutcome:
    """Apply one verified webhook. Does not commit.

    **Tenancy comes from the signed body, never from the caller.** The merchant
    is derived through `payment_attempts -> orders -> merchant_id`, reached from
    the payment id Razorpay signed. `asserted_merchant_id` is an *assertion* a
    caller may offer and is checked against that derivation; it can only cause a
    rejection, never a selection.

    This is the fix for a demonstrated defect. When the merchant was a caller
    supplied value it was possible to post a validly-signed webhook about one
    tenant's payment while naming another tenant: the event row was written
    under the caller's merchant, pointed at the victim's order and customer, and
    the victim's payment attempt was advanced. Existence-checking the merchant
    caught none of that, because the merchant existed — it simply was not the
    one that owned the payment.

    The caller owns the transaction, as everywhere else in this project: a
    repository or service that committed would decide on the caller's behalf
    that a partial run should survive.
    """
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise VerificationError(f"received_at must be timezone-aware, got {received_at!r}")

    name = payload.get("event")
    if not isinstance(name, str) or not name:
        raise VerificationError("webhook payload carries no event name")

    external_id = external_event_id_of(payload, header_event_id)

    if is_ignored(name):
        return WebhookOutcome(
            event_name=name,
            external_event_id=external_id,
            event_type=None,
            persisted=False,
            duplicate=False,
            ignored=True,
            unmapped=False,
            payment_attempt_updated=False,
        )

    try:
        event_type = map_event(name)
    except UnmappedEvent:
        # Recorded as a fact by the caller's log and acknowledged, never stored
        # under an invented meaning.
        return WebhookOutcome(
            event_name=name,
            external_event_id=external_id,
            event_type=None,
            persisted=False,
            duplicate=False,
            ignored=False,
            unmapped=True,
            payment_attempt_updated=False,
        )

    # Tenancy is established here, from the signed payment id, before anything
    # is written. An event RevTrace cannot attribute to a known payment is
    # refused rather than filed under a merchant the caller nominated.
    attempt = _payment_attempt_for(session, payload)
    if attempt is None:
        raise VerificationError(
            "no payment attempt matches this event's payment id, so the owning "
            "merchant cannot be established from signed data; refusing rather "
            "than trusting a caller-supplied merchant"
        )

    merchant_id = resolve_merchant(session, attempt)

    if asserted_merchant_id is not None and asserted_merchant_id != merchant_id:
        # The assertion disagrees with the signed data. Rejecting rather than
        # ignoring: a caller that names the wrong tenant is either mistaken or
        # probing, and neither should be answered with a silent success.
        raise VerificationError(
            "the supplied merchant does not own the payment this event is about"
        )

    if session.get(Merchant, merchant_id) is None:  # pragma: no cover - FK guarantees it
        raise VerificationError(f"no merchant {merchant_id}")

    occurred = occurred_at_of(payload, received_at=received_at)

    written = event_repository.insert_events(
        session,
        [
            {
                "merchant_id": merchant_id,
                "customer_id": attempt.customer_id,
                "order_id": attempt.order_id,
                "external_event_id": external_id,
                "event_type": event_type.value,
                "payload": _safe_payload(payload),
                "occurred_at": occurred,
                "received_at": received_at,
            }
        ],
    )
    persisted = written == 1

    previous = new = None
    updated = False
    if persisted:
        proposed = STATUS_FROM_EVENT.get(event_type)
        if proposed is not None:
            previous = attempt.status
            new = advance_status(previous, proposed.value)
            if new != previous:
                attempt.status = new
                updated = True

    return WebhookOutcome(
        event_name=name,
        external_event_id=external_id,
        event_type=event_type.value,
        persisted=persisted,
        duplicate=not persisted,
        ignored=False,
        unmapped=False,
        payment_attempt_updated=updated,
        previous_status=previous,
        new_status=new,
    )


#: Payload keys Razorpay may include that RevTrace has no use for and will not
#: store. Card details are the obvious one; `notes` is merchant-controlled free
#: text that can hold anything at all.
STRIPPED_KEYS = frozenset({"card", "notes", "acquirer_data", "token", "upi", "bank_details"})


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The stored copy, with fields RevTrace has no business keeping removed.

    The event row is evidence for a timeline, not an archive of everything a
    provider chose to send. Anything stripped here is something no RevTrace rule
    reads, and storing it would mean holding cardholder-adjacent data for no
    purpose.
    """
    stripped = _strip(payload)
    # `_strip` is recursive over Any, so the dict-ness of the top level is
    # re-established here rather than asserted by its return type.
    return stripped if isinstance(stripped, dict) else {}


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in STRIPPED_KEYS}
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


__all__ = [
    "STATUS_FROM_EVENT",
    "STATUS_RANK",
    "STRIPPED_KEYS",
    "VerificationError",
    "WebhookOutcome",
    "advance_status",
    "apply_webhook",
    "external_event_id_of",
    "occurred_at_of",
    "resolve_merchant",
]
