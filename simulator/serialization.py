"""Serialization to the three Phase 2 artifacts.

* ``fixture.json``  — canonical, complete, checksummed. The deterministic artifact.
* ``events.jsonl``  — the delivery stream, one delivery per line, for ingestion.
* ``frontend.json`` — a flattened read-optimized view-model for the UI.

Canonical JSON means sorted keys and no incidental whitespace, so that a
checksum over it is stable across runs and platforms. Timestamps are ISO-8601
UTC with a trailing ``Z``. Money stays an integer count of minor units on the
wire; formatting is the consumer's problem.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from simulator.models import (
    EntitySet,
    EventDelivery,
    GroundTruth,
    SimulationManifest,
    SimulationResult,
    SyntheticEvent,
)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _plain(value: Any) -> Any:
    """Convert dataclasses, UUIDs, and datetimes into JSON-safe primitives."""
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators."""
    return json.dumps(_plain(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_checksum(
    entities: EntitySet, deliveries: tuple[EventDelivery, ...], ground_truth: GroundTruth
) -> str:
    """SHA-256 over the canonical form of everything that varies with the seed.

    The manifest is excluded because it carries the checksum itself.
    """
    material = canonical_json(
        {
            "entities": entities,
            "deliveries": deliveries,
            "ground_truth": ground_truth,
        }
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def manifest_to_dict(manifest: SimulationManifest) -> dict[str, Any]:
    plain: dict[str, Any] = _plain(manifest)
    return plain


def fixture_to_dict(result: SimulationResult) -> dict[str, Any]:
    """The canonical fixture.json structure."""
    return {
        "manifest": _plain(result.manifest),
        "entities": _plain(result.entities),
        "deliveries": _plain(result.deliveries),
        "ground_truth": _plain(result.ground_truth),
    }


def events_jsonl(result: SimulationResult) -> str:
    """The delivery stream: one JSON object per line, in arrival order."""
    lines = [
        canonical_json({"delivery": delivery.envelope, "event": delivery.event})
        for delivery in result.deliveries
    ]
    return "\n".join(lines) + ("\n" if lines else "")


# -- frontend view-model --------------------------------------------------

_EVENT_SUMMARIES = {
    "order.created": "Order created",
    "order.paid": "Order paid",
    "checkout.started": "Checkout started",
    "checkout.abandoned": "Checkout abandoned",
    "payment.attempted": "Payment attempted",
    "payment.authorized": "Payment authorized",
    "payment.captured": "Payment captured",
    "payment.failed": "Payment failed",
    "subscription.charged": "Subscription charged",
    "subscription.payment_failed": "Subscription payment failed",
    "subscription.halted": "Subscription halted",
    "refund.created": "Refund created",
    "recovery.action_executed": "Recovery action executed",
    "recovery.succeeded": "Recovery succeeded",
    "recovery.failed": "Recovery failed",
}


def _summarize(event: SyntheticEvent) -> str:
    base = _EVENT_SUMMARIES.get(event.event_type, event.event_type)
    failure_code = event.payload.get("failure_code")
    if failure_code:
        return f"{base} — {failure_code}"
    return base


def frontend_view(result: SimulationResult) -> dict[str, Any]:
    """A flattened, read-optimized shape for the dashboard.

    `revenue_risk` and `recovery_state` are present and explicitly null in
    Phase 2: the shape is final so the UI can be built against it, but the
    simulator has no authority to populate either. Phase 3 fills revenue_risk;
    Phases 6-9 fill recovery_state.
    """
    entities = result.entities
    merchant = entities.merchants[0] if entities.merchants else None
    currency = result.manifest.currency

    captured_minor = sum(
        attempt.amount
        for attempt in entities.payment_attempts
        if attempt.status in {"captured", "refunded"}
    )
    at_risk_minor = sum(risk.amount_at_risk for risk in result.ground_truth.expected_risks)

    timeline = [
        {
            "sequence": delivery.envelope.sequence,
            "occurred_at": _iso(delivery.event.occurred_at),
            "received_at": _iso(delivery.event.received_at),
            "event_type": delivery.event.event_type,
            "external_event_id": delivery.event.external_event_id,
            "order_ref": delivery.event.payload.get("order_ref"),
            "is_duplicate": delivery.envelope.is_duplicate,
            "is_out_of_order": delivery.envelope.is_out_of_order,
            "is_delayed": delivery.envelope.is_delayed,
            "delay_seconds": delivery.envelope.delay_seconds,
            "summary": _summarize(delivery.event),
        }
        for delivery in result.deliveries
    ]

    payment_attempts = [
        {
            "attempt_number": attempt.attempt_number,
            "payment_ref": attempt.external_payment_id,
            "status": attempt.status,
            "method": attempt.payment_method,
            "amount_minor": attempt.amount,
            "currency": attempt.currency,
            "failure_code": attempt.failure_code,
            "failure_reason": attempt.failure_reason,
            "attempted_at": _iso(attempt.attempted_at),
        }
        for attempt in sorted(
            entities.payment_attempts, key=lambda a: (a.attempted_at, a.attempt_number)
        )
    ]

    return {
        "summary": {
            "scenario_id": result.manifest.scenario_id,
            "scenario_name": result.manifest.scenario_name,
            "merchant_name": merchant.name if merchant else None,
            "currency": currency,
            "orders_total": len(entities.orders),
            "customers_total": len(entities.customers),
            "amount_captured_minor": captured_minor,
            "amount_at_risk_minor": at_risk_minor,
            "events_total": len(result.deliveries),
            "events_unique": result.ground_truth.expected_persisted_event_count,
        },
        "orders": [
            {
                "order_ref": order.external_order_id,
                "amount_minor": order.amount,
                "currency": order.currency,
                "status": order.status,
            }
            for order in entities.orders
        ],
        "timeline": timeline,
        "payment_attempts": payment_attempts,
        # Phase 3 populates this. Null in Phase 2 by design.
        "revenue_risk": None,
        # Phases 6-9 populate this. Null in Phase 2 by design.
        "recovery_state": None,
    }
