# 0005 — The simulator generates no risk or recovery entities

- **Status:** Accepted
- **Date:** 2026-08-26
- **Phase:** 2

## Context

Phase 2 needed recovery-success and recovery-failure scenarios so that Phase 11
can eventually measure expected recovery against actual recovery.

The Phase 1 `EventType` vocabulary already contains
`recovery.action_executed`, `recovery.succeeded`, and `recovery.failed`, so the
*events* are representable. The temptation is to go further and generate the
`recovery_cases` and `recovery_actions` rows that would accompany them in a real
run.

That would mean the simulator writing `policy_status='approved'`,
`approved=true`, and `executed=true` — asserting an authorization it has no
right to assert. The architecture rule is that deterministic policy code, not
anything else, authorizes money movement. A test-data generator fabricating
approvals is a violation of that rule even though no real money is involved,
because it puts fabricated authority records into the same tables the policy
engine will later be trusted to own.

A second, narrower problem surfaced at the same time. Scenario S10 — a payment
is captured but the order never reconciles to paid — has no matching value in
the Phase 1 `RiskType` vocabulary, which has exactly four members
(`repeated_payment_failure`, `checkout_abandonment`,
`subscription_payment_failure`, `payment_degradation`).

## Decision

**The simulator generates only these four entity types:** merchants, customers,
orders, and payment attempts. It generates no `revenue_risks`,
`recovery_cases`, `recovery_actions`, or `audit_events` — ever.

Recovery scenarios (S11, S12) emit `recovery.*` **events only**, as historical
facts within a synthetic timeline: "in this history, a payment-link recovery
action was executed and the payment then succeeded." No case record, no action
record, no approval, no policy decision.

Ground truth states **what should be detected** — a risk type, an amount at
risk in minor units, and a plain-language reason. It deliberately carries no
confidence, no score, no recommended action, and no expected-recovery figure.
Those are outputs of the deterministic risk and recovery engines in Phases 3
and 6; the simulator has no authority over them and does not guess at them.

**Scenario S10 is recorded as an untyped anomaly**, via a separate
`ExpectedAnomaly` record carrying an `anomaly_kind` string rather than a
`RiskType`. Phase 3 decides how to classify and detect it, and whether the
`RiskType` vocabulary should grow. Phase 2 does not pre-empt that with a schema
change.

Enforced by tests: no payload may contain `approved`, `policy_status`, or
`approved_by`; `ExpectedRisk` carries no confidence or recommendation field; and
every `risk_type` in ground truth must be a member of the Phase 1 enum.

## Consequences

**Easy:** the authority boundary stays intact with no ambiguity about who
authorized what. Phases 6–9 inherit clean tables that only the policy engine
ever writes to.

**Harder:** Phase 11 evaluation cannot read actual-recovery figures straight out
of simulated case records — it must derive them from the event timeline. That is
more work, and it is also more honest, since it exercises the same
reconstruction path production would use.

**Deferred:** if a later phase genuinely needs synthetic `recovery_cases` (for
UI development, say), that is a separate decision requiring its own approval —
not something Phase 2 grants by default.

## Alternatives considered

**Generate full recovery case records.** Rejected on the authority-boundary
grounds above.

**Add a fifth `RiskType` for reconciliation mismatches.** Rejected for Phase 2.
It would require a Phase 1 schema change and a new migration to support a
scenario that no detector consumes yet. The vocabulary should grow when a
detector needs it, not in anticipation.

**Omit S10, S11, and S12 entirely.** Rejected. The event shapes are genuinely
useful to Phase 3 and Phase 11, and they are fully representable within the
existing schema. Only the entity records were problematic.
