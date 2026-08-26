# 0002 — UUID primary keys

- **Status:** Accepted
- **Date:** 2026-08-26
- **Phase:** 1

## Context

Every RevTrace entity needs a primary key. The system is audit-heavy: audit
events reference cases, cases reference risks, risks reference orders and
customers, and the Revenue Leak Graph stitches these together into timelines
that are shown in the UI and exported into evaluation runs.

Identifiers therefore appear in URLs, in audit snapshots, in AI evidence
payloads, and in cross-referenced JSON. Two properties matter: they must be
generatable before insert (so a graph of related objects can be assembled in
memory), and they must not leak information.

## Decision

All primary keys are **UUID v4**, stored in PostgreSQL's native `uuid` type,
generated application-side via `uuid.uuid4` in `UUIDPrimaryKeyMixin`.

Provider identifiers (`external_order_id`, `external_payment_id`,
`external_event_id`, `external_customer_id`) are separate nullable columns with
their own uniqueness constraints. Razorpay's identifiers are never our primary
key.

## Consequences

**Easy:** object graphs can be built before any database round-trip;
identifiers are safe to expose in URLs and AI evidence payloads without leaking
business volume; merging data from the simulator and from live Razorpay test
activity cannot collide.

**Harder:** 16 bytes per key rather than 8, and random UUIDs have worse index
locality than sequential integers under heavy insert load. At hackathon data
volumes — thousands of simulated events — this is not a practical concern. If
it ever becomes one, UUID v7 preserves the interface while restoring time
ordering.

**Ruled out:** inferring anything from key ordering. `created_at` and
`occurred_at` are the only time signals, which is correct anyway given that
events arrive out of order.

## Alternatives considered

**`BIGSERIAL`.** Smaller, faster to index, better locality. Lost on two counts:
sequential integers in URLs leak how many orders or cases exist, which is
commercially meaningful information; and they require a database round-trip
before a related object can reference them, which complicates assembling a leak
graph in memory.

**UUID v7.** Time-ordered, so it keeps index locality while remaining opaque.
A reasonable future migration, but it adds a dependency or hand-rolled
generation for a problem this project does not have yet.
