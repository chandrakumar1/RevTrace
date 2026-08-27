# 0004 — The simulator emits an event stream, not database writes

- **Status:** Accepted
- **Date:** 2026-08-26
- **Phase:** 2

## Context

Phase 2 needed a synthetic event source. The obvious shortcut is to have the
simulator insert rows into `revtrace_dev` directly — one step instead of two,
and the data is immediately queryable.

The specification's pipeline names `EVENT INGESTION` as its first stage, and
`app/services/ingestion/` is deliberately empty until Phase 3. So the question
is really: does the simulator feed that stage, or replace it?

The duplicate-webhook requirement settles it. RevTrace must tolerate duplicate
delivery, and the Phase 1 schema enforces that with
`UNIQUE(merchant_id, external_event_id)`. If the simulator writes rows itself,
it hits that constraint during its own generation and has to catch and swallow
the error — which makes the constraint the *simulator's* problem rather than the
behaviour under test.

## Decision

`simulate(scenario, seed=...)` returns an in-memory `SimulationResult` and
performs no I/O whatsoever: no database connection, no network, no filesystem
access. Serialization is a separate module; persistence is a separate phase.

Three artifacts are produced from a result:

| Artifact | Purpose |
|---|---|
| `fixture.json` | Canonical, complete, checksummed. The deterministic record. |
| `events.jsonl` | The delivery stream, one delivery per line, for ingestion. |
| `frontend.json` | Flattened read-optimized view-model for the dashboard. |

Duplicates are **emitted** into the stream and **rejected** by storage. The
stream is a delivery log, and delivery logs legitimately contain redeliveries.
The simulator never deduplicates.

Ground truth is serialized to its own section, never into event payloads, so
that a detector physically cannot read the answer out of its own input.

## Consequences

**Easy:** the entire simulator test suite is hermetic — 1,587 test cases run in
about ten seconds with no database, no fixtures, and no teardown. Output is
byte-comparable and checksummable. `cat events.jsonl` beats a psql session for
debugging. `revtrace_dev` stays clean.

**Harder:** getting simulated data into PostgreSQL requires the ingestion layer
to exist. That is the correct dependency direction, but it does mean Phase 2
alone cannot demonstrate end-to-end persistence.

**Enabled:** Phase 3 can test idempotency properly, because it receives
unpersisted duplicates as input and can assert that the constraint suppresses
them.

## Alternatives considered

**Direct database inserts.** Rejected. It collapses two pipeline stages into
one, makes every test require PostgreSQL, accumulates synthetic rows in the only
approved database with no cleanup story, and renders the duplicate-webhook
scenario incoherent.

**A hybrid — generate in memory, with an optional `--persist` flag.** Deferred
rather than rejected. The flag would belong to the ingestion layer in Phase 3,
not to the simulator, and adding it now would prejudge that layer's interface.
