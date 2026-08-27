# 0006 — Ingestion accepts entities and events together

- **Status:** Accepted
- **Date:** 2026-08-27
- **Phase:** 3

## Context

[ADR 0004](0004-simulator-emits-event-stream-not-database-writes.md) made the
simulator emit an event stream and left persistence to Phase 3. The obvious
reading of that is an ingestion endpoint that accepts events and nothing else —
one webhook-shaped door, exactly as production will eventually look.

That does not survive contact with the Phase 1 schema. `events.merchant_id` is
`NOT NULL` with a foreign key, and `events.order_id` and `events.customer_id`
are foreign keys too. An events-only endpoint has three options when an event
arrives for a merchant that does not exist yet:

1. reject it — which makes the simulator unusable, since its first event is
   always for a merchant nobody has created;
2. create the missing rows from the event payload — which means **inventing
   entity facts**: a merchant name, a timezone, a customer's lifetime value,
   none of which are in an `order.created` payload;
3. drop the foreign keys — which discards the referential integrity the leak
   graph depends on.

Option 2 is the dangerous one, because it looks reasonable. Manufacturing a
merchant row out of a payload fragment produces a record that claims to be an
observation and is actually a guess.

## Decision

**`POST /api/v1/ingest/simulation` accepts `{entities, deliveries}` as one
document.** Entities are upserted first, in foreign-key order (merchants →
customers → orders → payment_attempts), then events are appended.

Four properties are enforced at this boundary:

**Ground truth cannot enter.** A body carrying `ground_truth` is rejected with a
specific error, and every model sets `extra="forbid"`. Nine further keys —
`expected_risks`, `risk_type`, `amount_at_risk`, `narrative`, `scenario_id` and
others — are rejected inside event payloads. Detection therefore cannot read the
answer from its own input by construction, not by discipline.

**References are validated before anything is written.** An event pointing at a
merchant or order that is neither in the batch nor already stored rejects the
whole request with `422`. A mid-batch integrity error would otherwise leave a
torn timeline that later reconstruction would silently read as missing events.

**Nothing partial is written.** Validation is all-or-nothing, and the whole
operation runs in the caller's transaction.

**Duplicates are offered to the database, not filtered in application code.**
`UNIQUE(merchant_id, external_event_id)` suppresses them, and the response
reports `duplicates_suppressed`. Re-posting an identical fixture is a no-op:
`events_received: 7, events_persisted: 0, duplicates_suppressed: 7`.

The delivery envelope (`sequence`, `is_duplicate`, `delay_seconds`,
`is_out_of_order`) is accepted for completeness and **deliberately not
persisted**. Every fact it carries is re-derivable from the stored
`occurred_at` / `received_at` pair, and storing derivable state inside an
append-only table means storing a second version of the truth that can disagree
with the first.

This endpoint is explicitly a **development and evaluation door**, named
`/ingest/simulation` rather than `/ingest/events` so it cannot be mistaken for
the Razorpay webhook receiver. That arrives in Phase 9, takes a signed provider
payload, and will not accept entities.

## Consequences

**Easy:** a fixture round-trips in one request. Ingestion reaches exactly five
tables — merchants, customers, orders, payment_attempts, events — and has no
import path to `revenue_risks`, `recovery_cases`, `recovery_actions`, or
`audit_events`, so simulated input can never manufacture a risk or an approval.
A test asserts that absence.

**Harder:** this is not the shape a webhook takes, so Phase 9 writes a second
ingestion path rather than reusing this one. That is the correct cost — the two
have genuinely different trust models, and collapsing them would mean the
webhook receiver inheriting an endpoint that accepts arbitrary entity rows.

**Ruled out:** creating entity rows from event payloads, anywhere in the system.

## Alternatives considered

**Events-only, with entities auto-created from payloads.** Rejected: it
fabricates facts, as above.

**Two endpoints, one for entities and one for events.** Rejected. It makes the
all-or-nothing guarantee impossible across the pair — a caller could load
entities, fail on events, and leave a half-populated merchant that looks
complete.

**Persist the delivery envelope alongside each event.** Rejected. It duplicates
derivable state in an append-only table. `IntegrityFlags` recomputes duplicate,
delay, and out-of-order counts from timestamps at read time instead.
