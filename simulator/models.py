"""In-memory simulation data structures.

Everything here is a frozen dataclass with no I/O, no database, and no
serialization concerns. `SimulationResult` is the primary product of the
simulator; persisting or serializing it is somebody else's job
(`serialization.py` for files, the Phase 3 ingestion layer for PostgreSQL).

Every field maps onto the Phase 1 schema, or onto delivery metadata that is
deliberately *not* part of the schema. Money is integer minor units throughout.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

# -- entities -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyntheticMerchant:
    """Maps to the `merchants` table."""

    id: uuid.UUID
    external_ref: str
    name: str
    currency: str
    timezone: str


@dataclass(frozen=True, slots=True)
class SyntheticCustomer:
    """Maps to the `customers` table."""

    id: uuid.UUID
    merchant_id: uuid.UUID
    external_customer_id: str
    name: str
    email: str
    phone: str
    #: Integer minor units.
    lifetime_value: int
    contactable: bool
    contact_count: int


@dataclass(frozen=True, slots=True)
class SyntheticOrder:
    """Maps to the `orders` table."""

    id: uuid.UUID
    merchant_id: uuid.UUID
    customer_id: uuid.UUID | None
    external_order_id: str
    #: Integer minor units.
    amount: int
    currency: str
    status: str


@dataclass(frozen=True, slots=True)
class SyntheticPaymentAttempt:
    """Maps to the `payment_attempts` table."""

    id: uuid.UUID
    order_id: uuid.UUID
    customer_id: uuid.UUID | None
    external_payment_id: str
    #: Integer minor units.
    amount: int
    currency: str
    payment_method: str
    #: Never "razorpay" — nothing here came from Razorpay.
    provider: str
    status: str
    failure_code: str | None
    failure_reason: str | None
    attempt_number: int
    attempted_at: datetime


@dataclass(frozen=True, slots=True)
class EntitySet:
    """All domain entities produced by one simulation run."""

    merchants: tuple[SyntheticMerchant, ...] = ()
    customers: tuple[SyntheticCustomer, ...] = ()
    orders: tuple[SyntheticOrder, ...] = ()
    payment_attempts: tuple[SyntheticPaymentAttempt, ...] = ()

    def order_by_id(self, order_id: uuid.UUID) -> SyntheticOrder | None:
        return next((o for o in self.orders if o.id == order_id), None)


# -- events ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyntheticEvent:
    """Maps 1:1 onto an `events` row.

    `payload` carries only what a provider webhook would carry. Ground truth,
    scenario identity, and risk information never appear here — detection must
    not be able to read the answer from its own input.
    """

    id: uuid.UUID
    merchant_id: uuid.UUID
    customer_id: uuid.UUID | None
    order_id: uuid.UUID | None
    external_event_id: str
    event_type: str
    payload: dict[str, object]
    #: When it actually happened. Causal truth.
    occurred_at: datetime
    #: When RevTrace received it. Always >= occurred_at.
    received_at: datetime


@dataclass(frozen=True, slots=True)
class DeliveryEnvelope:
    """Delivery metadata. Deliberately NOT part of the `events` row.

    This is simulation bookkeeping about *how* an event arrived, kept separate
    from the domain event itself.
    """

    #: Position in delivery order, 1-based.
    sequence: int
    #: 1 for a first delivery, 2+ for a redelivery of the same event.
    delivery_attempt: int
    is_duplicate: bool
    is_delayed: bool
    delay_seconds: int
    #: True when this event arrived after an event that occurred later than it.
    is_out_of_order: bool


@dataclass(frozen=True, slots=True)
class EventDelivery:
    """One arrival of one event. Duplicates share an event but differ here."""

    envelope: DeliveryEnvelope
    event: SyntheticEvent


# -- ground truth ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedRisk:
    """A risk a correct Phase 3 detector should find.

    `risk_type` is always a member of the Phase 1 `RiskType` enum. Amounts are
    integer minor units. There is deliberately no confidence, score, or
    recommendation here — the simulator has no authority over those.
    """

    risk_type: str
    amount_at_risk: int
    currency: str
    order_ref: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ExpectedAnomaly:
    """An inconsistency with no `RiskType` in the Phase 1 schema.

    Used for scenario S10, where a payment is captured but the order never
    reconciles. Phase 3 decides how (and whether) to classify it; Phase 2 only
    records that it is there.
    """

    anomaly_kind: str
    order_ref: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """What a correct downstream implementation should conclude.

    Kept strictly outside event payloads and serialized to its own file, so
    that detection physically cannot consume it as input.
    """

    expected_risks: tuple[ExpectedRisk, ...] = ()
    expected_anomalies: tuple[ExpectedAnomaly, ...] = ()
    #: Total deliveries emitted, including duplicates.
    emitted_event_count: int = 0
    #: Rows that should exist after idempotent ingestion. Lower when duplicated.
    expected_persisted_event_count: int = 0
    #: external_event_ids generated but never delivered.
    dropped_events: tuple[str, ...] = ()
    #: external_event_ids delivered more than once.
    duplicated_events: tuple[str, ...] = ()
    #: Free-text description of what the scenario represents.
    narrative: str = ""


# -- manifest and result --------------------------------------------------


@dataclass(frozen=True, slots=True)
class SimulationManifest:
    """Reproduction metadata for one run."""

    scenario_id: str
    scenario_name: str
    category: str
    seed: int
    generator_version: str
    epoch: datetime
    currency: str
    counts: dict[str, int] = field(default_factory=dict)
    window_start: datetime | None = None
    window_end: datetime | None = None
    #: sha256 over the canonical serialization of entities + deliveries + ground truth.
    checksum: str = ""


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """The product of `simulate()`. Pure in-memory, no I/O."""

    manifest: SimulationManifest
    entities: EntitySet
    #: Delivery-ordered. MAY contain duplicates — never silently deduplicated.
    deliveries: tuple[EventDelivery, ...]
    ground_truth: GroundTruth

    @property
    def events_in_causal_order(self) -> tuple[SyntheticEvent, ...]:
        """Unique events sorted by `occurred_at` — the reconstructed timeline.

        This is how a timeline is always rebuilt: by when things happened, never
        by the order they arrived.
        """
        seen: set[str] = set()
        unique: list[SyntheticEvent] = []
        for delivery in self.deliveries:
            if delivery.event.external_event_id in seen:
                continue
            seen.add(delivery.event.external_event_id)
            unique.append(delivery.event)
        return tuple(sorted(unique, key=lambda e: (e.occurred_at, e.external_event_id)))
