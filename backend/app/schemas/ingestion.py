"""Ingestion request and response schemas.

This module is the boundary where simulated data becomes real rows, so it is
also where the two structural guarantees are enforced:

* **Ground truth cannot enter.** A payload carrying `ground_truth` is rejected
  outright with a specific error, and `extra="forbid"` catches anything else
  unexpected. Detection therefore cannot read the answer from its own input —
  by construction, not by discipline.
* **Nothing partial is written.** Validation is all-or-nothing: a single
  malformed event rejects the whole batch, because a half-ingested delivery
  stream would produce a torn timeline that later reconstruction would silently
  treat as missing events.

Timestamps must be timezone-aware; money must be integer minor units.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from app.models.enums import EventType, OrderStatus, PaymentMethod, PaymentStatus

#: Maximum length of the external_event_id column in the Phase 1 schema.
MAX_EXTERNAL_ID_LENGTH = 128

#: Keys that would carry evaluation answers into detection input.
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "ground_truth",
        "expected_risks",
        "expected_risk",
        "expected_anomalies",
        "risk_type",
        "amount_at_risk",
        "narrative",
        "scenario",
        "scenario_id",
    }
)


def _require_utc(value: datetime, field_name: str) -> datetime:
    """Reject naive datetimes; normalise any aware datetime to UTC.

    An offset timezone is accepted and converted rather than refused — the
    instant is unambiguous, and normalising here means everything downstream
    compares in one timezone.
    """
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware; got a naive datetime")
    return value.astimezone(UTC)


def _require_positive_int_money(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{field_name} must be an integer count of minor units, got {type(value).__name__}"
        )
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative, got {value}")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- entities -------------------------------------------------------------


class MerchantIn(_Strict):
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    currency: str = Field(min_length=3, max_length=3)
    timezone: str = Field(min_length=1, max_length=64)
    #: Present in simulator output; the Phase 1 merchants table has no column
    #: for it, so it is accepted and ignored rather than rejected.
    external_ref: str | None = None


class CustomerIn(_Strict):
    id: uuid.UUID
    merchant_id: uuid.UUID
    external_customer_id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)
    lifetime_value: StrictInt
    contactable: bool = True
    contact_count: int = 0

    @field_validator("lifetime_value")
    @classmethod
    def _money(cls, v: int) -> int:
        return _require_positive_int_money(v, "lifetime_value")


class OrderIn(_Strict):
    id: uuid.UUID
    merchant_id: uuid.UUID
    customer_id: uuid.UUID | None = None
    external_order_id: str | None = Field(default=None, max_length=128)
    amount: StrictInt
    currency: str = Field(min_length=3, max_length=3)
    status: str = Field(max_length=32)

    @field_validator("amount")
    @classmethod
    def _money(cls, v: int) -> int:
        return _require_positive_int_money(v, "amount")

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in OrderStatus.values():
            raise ValueError(f"invalid order status {v!r}")
        return v


class PaymentAttemptIn(_Strict):
    id: uuid.UUID
    order_id: uuid.UUID
    customer_id: uuid.UUID | None = None
    external_payment_id: str | None = Field(default=None, max_length=128)
    amount: StrictInt
    currency: str = Field(min_length=3, max_length=3)
    payment_method: str = Field(max_length=32)
    provider: str = Field(max_length=64)
    status: str = Field(max_length=32)
    failure_code: str | None = Field(default=None, max_length=128)
    failure_reason: str | None = None
    attempt_number: int = Field(ge=1)
    attempted_at: datetime

    @field_validator("amount")
    @classmethod
    def _money(cls, v: int) -> int:
        return _require_positive_int_money(v, "amount")

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in PaymentStatus.values():
            raise ValueError(f"invalid payment status {v!r}")
        return v

    @field_validator("payment_method")
    @classmethod
    def _method(cls, v: str) -> str:
        if v not in PaymentMethod.values():
            raise ValueError(f"invalid payment method {v!r}")
        return v

    @field_validator("attempted_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc(v, "attempted_at")


class EntitySetIn(_Strict):
    merchants: list[MerchantIn] = Field(default_factory=list)
    customers: list[CustomerIn] = Field(default_factory=list)
    orders: list[OrderIn] = Field(default_factory=list)
    payment_attempts: list[PaymentAttemptIn] = Field(default_factory=list)


# -- events ---------------------------------------------------------------


class DeliveryEnvelopeIn(_Strict):
    """Delivery metadata. Accepted for completeness, deliberately not persisted.

    Duplicate, delay, and out-of-order facts are re-derivable from the
    persisted occurred_at / received_at pair, so storing the envelope would
    duplicate derivable state inside an append-only table.
    """

    sequence: int = Field(ge=1)
    delivery_attempt: int = Field(ge=1)
    is_duplicate: bool = False
    is_delayed: bool = False
    delay_seconds: int = Field(default=0, ge=0)
    is_out_of_order: bool = False


class EventIn(_Strict):
    id: uuid.UUID
    merchant_id: uuid.UUID
    customer_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    external_event_id: str = Field(min_length=1, max_length=MAX_EXTERNAL_ID_LENGTH)
    event_type: str = Field(max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
    received_at: datetime

    @field_validator("event_type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in EventType.values():
            raise ValueError(f"unknown event_type {v!r}")
        return v

    @field_validator("occurred_at", "received_at")
    @classmethod
    def _utc(cls, v: datetime, info: Any) -> datetime:
        return _require_utc(v, str(info.field_name))

    @field_validator("payload")
    @classmethod
    def _clean_payload(cls, v: dict[str, Any]) -> dict[str, Any]:
        leaked = FORBIDDEN_PAYLOAD_KEYS & set(v)
        if leaked:
            raise ValueError(
                f"payload carries evaluation-only keys {sorted(leaked)}; "
                "ground truth must never reach detection input"
            )
        for key, value in v.items():
            if key.endswith("_minor"):
                _require_positive_int_money(value, f"payload.{key}")
        return v

    @model_validator(mode="after")
    def _delivery_after_occurrence(self) -> EventIn:
        if self.received_at < self.occurred_at:
            raise ValueError(
                f"received_at ({self.received_at.isoformat()}) precedes occurred_at "
                f"({self.occurred_at.isoformat()}) for {self.external_event_id}"
            )
        return self


class DeliveryIn(_Strict):
    envelope: DeliveryEnvelopeIn
    event: EventIn


# -- request / response ---------------------------------------------------


class SimulationIngestRequest(_Strict):
    """A simulator fixture, minus its answer key.

    `manifest` is optional and echoed back for traceability; it carries only
    reproduction metadata (scenario, seed, checksum), never expectations.
    """

    entities: EntitySetIn
    deliveries: list[DeliveryIn] = Field(default_factory=list)
    manifest: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_ground_truth(cls, data: Any) -> Any:
        if isinstance(data, dict) and "ground_truth" in data:
            raise ValueError(
                "ground_truth must not be submitted to ingestion: it is evaluation "
                "data and must never reach detection input. Strip it and resubmit."
            )
        return data


class IngestionResponse(BaseModel):
    merchants_upserted: int
    customers_upserted: int
    orders_upserted: int
    payment_attempts_upserted: int
    events_received: int
    events_persisted: int
    duplicates_suppressed: int
    scenario_id: str | None = None
    seed: int | None = None
