# 0007 — `reconciliation_mismatch`, with zero amount at risk

- **Status:** Accepted
- **Date:** 2026-08-27
- **Phase:** 3

## Context

Scenario S10 captures a payment whose order never reconciles to paid. Every
event that occurred was delivered; the terminal `order.paid` genuinely never
happened.

[ADR 0005](0005-simulator-does-not-generate-recovery-or-risk-entities.md)
deferred this deliberately: Phase 2 recorded S10 as an **untyped anomaly**,
carrying an `anomaly_kind` string rather than a `RiskType`, on the grounds that
"the vocabulary should grow when a detector needs it, not in anticipation."

Phase 3 is where a detector needs it. Two questions had to be answered together,
and the second is the one that matters.

## Decision

### A fifth `RiskType`, added additively

`RiskType.RECONCILIATION_MISMATCH = "reconciliation_mismatch"`, introduced by a
new Alembic migration (`bbc0a1ffda2c`) that drops and recreates the
`ck_revenue_risks_risk_type_valid` CHECK constraint with the widened vocabulary.

The Phase 1 migration `343997466c88` is **not modified**. It is applied history;
editing it would mean two databases claiming the same revision with different
schemas.

This is exactly the growth path [ADR 0003](0003-status-columns-as-varchar-with-check-constraints.md)
chose VARCHAR + CHECK for, and it is why the Phase 1 test asserting the four
specification scenarios was corrected from exact set equality to a
subset/presence check. The specification's Scenario A–D vocabulary is a floor,
not a ceiling; a test that forbids the vocabulary from ever growing was testing
the wrong intent.

### `amount_at_risk = 0` — the substantive half

**The money arrived.** What is broken is bookkeeping, not revenue.

Reporting the order amount here would inflate every at-risk total in the product
with funds that were actually collected — the headline "revenue at risk" figure
on the dashboard would count captured money as lost. That is not a conservative
error. It overstates the problem the product exists to solve, and it would
overstate the recovery that later "fixing" it appears to achieve.

`RECONCILIATION_AMOUNT_AT_RISK = 0` is a named constant in `risk_engine.py`
rather than a bare literal, precisely so that this is a documented decision
rather than something a future reader repairs as an oversight. The captured
amount stays fully visible in the evidence response, so nothing is hidden — it
is simply not counted as at risk.

### A grace period, not an immediate finding

The detector fires only after `reconciliation_grace_seconds` (default one hour)
has elapsed since the last capture, measured against the supplied `as_of` and
never a clock. An `order.paid` still in flight is a **late** event, not a
missing one, and the specification is explicit that late delivery must be
tolerated. Without the grace period this detector would report every slow
webhook as a mismatch.

Resolution closes the loop: if `order.paid` arrives afterwards, the risk moves
to `recovered` with `amount_recovered = 0` and the reason "the event was late,
not missing."

## Consequences

**Easy:** S10 is now a first-class detectable risk with a versioned rule
(`reconciliation_mismatch.v1`), and integrity problems are visible without
polluting revenue figures.

**Harder:** consumers must not assume `amount_at_risk > 0` for every risk. The
API contract documents this explicitly, and `total_amount_at_risk_minor` in a
detection-run summary can legitimately be `0` while `risks_created` is `1`.

**A live question for Phase 6.** A zero-amount risk has no expected recovery to
compute, so the counterfactual recovery engine will need an explicit
non-monetary path for it — the appropriate response is a reconciliation
investigation, not a payment retry.

## Alternatives considered

**Report the order amount as at risk.** Rejected on the inflation grounds above.
This was the tempting option: it makes the demo numbers larger.

**Leave it untyped and report it outside `revenue_risks`.** Rejected. It is a
detected problem with evidence, a confidence score, and a resolution path; a
parallel table for one anomaly type would fragment the model for no gain.

**Use a native PostgreSQL ENUM and `ALTER TYPE ... ADD VALUE`.** Rejected in
[ADR 0003](0003-status-columns-as-varchar-with-check-constraints.md) already;
this decision is the first case that exercised the reason.

**Modify the Phase 1 migration to include the new value.** Rejected outright.
Applied migrations are history.
