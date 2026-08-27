# 0008 — Evidence is derived at read time, not stored on the risk

- **Status:** Accepted
- **Date:** 2026-08-27
- **Phase:** 3

## Context

A detected risk needs to be explainable: which attempts failed, with what
failure codes, in what order, and which events support the conclusion. The
Investigation View in Phase 10 is built entirely out of that material, and
Phase 5's AI diagnosis receives it as structured evidence.

The natural implementation is a JSONB `evidence` column on `revenue_risks`,
written when the detector fires. It is one query, it is fast, and it is what
most systems do.

It also creates a second copy of the truth.

## Decision

**Nothing is stored beyond what the detector concluded.** `revenue_risks` holds
`risk_type`, `amount_at_risk`, `currency`, `confidence_bps`, `detection_rule`,
`detected_at`, and `status`. Evidence is reconstructed from `events` on demand
and served by its own endpoint, `GET /api/v1/risks/{risk_id}/evidence`.

The reasoning is that `events` is **append-only** — it has `created_at`, no
`updated_at`, and no update path. Evidence re-derived from the same immutable
rows, by the same versioned rule, at the same `detected_at`, is stable. A stored
snapshot is stable too, right up until the two disagree, and then there is no
principled way to say which one is right.

Concretely, a stored snapshot goes stale the moment a delayed event arrives. In
S09 the late `payment.failed` lands after detection has already run. Derived
evidence includes it on the next read. A stored blob would not, and would keep
presenting a confidently incomplete picture — the worst of both.

`detection_rule` is versioned (`repeated_payment_failure.v1`) so a future rule
change is visible rather than silent. Re-derivation under a newer rule is
detectable by comparing the stored rule identifier to the current one.

Two consequences of derivation are surfaced honestly rather than hidden:

**`current_reason` is nullable.** A resolved risk no longer fires, so there is
legitimately no current reason to report. The field reads `null` and says so,
rather than replaying a reason that no longer holds.

**`duplicate_deliveries` reads `0` for anything rebuilt from stored rows.** The
unique constraint rejected the redeliveries at ingestion, so they never became
rows. That is a property of the source, not a defect, and the response schema
documents it. Duplicate counts are visible in the ingestion response
(`duplicates_suppressed`), which is where they are actually observed.

## Consequences

**Easy:** evidence cannot drift from the events it describes, because there is
only one copy. Detection stays a pure function of the timeline plus `as_of`, and
`revenue_risks` stays narrow.

**Harder:** each evidence read costs a query plus a reconstruction. Acceptable
now — a single order's timeline is tens of events — and unacceptable at real
volume. `GET /api/v1/risks/{risk_id}` deliberately does **not** inline evidence,
so a list-then-detail flow never pays that cost; only an explicit evidence fetch
does. The obvious hardening when volume arrives is a cache keyed on
`(order_id, max(occurred_at), detection_rule)`, which is a cache and not a
second source of truth.

**Deferred:** if Phase 5 needs the exact evidence an AI diagnosis was given —
and it will, for the audit trail — that belongs in `audit_events.input_snapshot`,
which already exists and is append-only. Recording what was *shown to a
decision-maker at a moment* is a different thing from caching a derivation, and
it is the audit trail's job.

## Alternatives considered

**JSONB `evidence` column on `revenue_risks`.** Rejected on the drift grounds
above.

**Denormalised evidence table written at detection time.** Same problem with
more machinery.

**Inline evidence in the risk detail response.** Rejected. It makes every detail
fetch pay for a reconstruction, including the common case of rendering a list.
The separate endpoint plus an `evidence_url` pointer keeps the cost opt-in.
