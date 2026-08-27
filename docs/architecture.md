# RevTrace — Architecture

**Status: Phase 1 complete (schema pending migration application).** Sections
are added per milestone as each component is actually built.

The authoritative specification is [../CLAUDE.md](../CLAUDE.md). This document
records what was *actually built* and why it diverged, if it did.

---

## Shape

A clean modular monolith. No microservices.

- `backend/` — Python 3.13, FastAPI, SQLAlchemy, Pydantic, PostgreSQL, Alembic
- `frontend/` — React, Vite, TypeScript, Tailwind, Recharts (Phase 10)
- `simulator/` — synthetic event generator, dev/eval only (Phase 2)

## Core pipeline

```
EVENT INGESTION
  -> DETECTION
  -> REVENUE RISK
  -> REVENUE LEAK GRAPH
  -> AI DIAGNOSIS
  -> INTERVENTION SIMULATION
  -> POLICY GATE
  -> EXECUTION
  -> VERIFICATION
  -> AUDIT
  -> METRICS
```

## The authority boundary

The single most important rule in this system: **the LLM is not the authority
over money.**

| LLM may do | Deterministic code must own |
|---|---|
| Diagnosis | Revenue calculations |
| Evidence interpretation | Risk calculations |
| Reasoning | Expected-recovery calculations |
| Explanation | Policy enforcement |
| Recommendation among *permitted* actions | Limits, retry counts, spend/discount caps |
| Communication drafting | Stopping rules |
| | Execution authorization |
| | Verification |
| | Metrics |

The LLM never directly executes a Razorpay operation. Every financial action
passes through:

```
AI recommendation -> deterministic validation -> policy engine -> approved action
    -> Razorpay adapter -> verification -> audit log
```

Structurally this means `app/agents/` may only ever *return structured
recommendations*. `app/engine/` and `app/services/` decide and act.

## Data model (Phase 1)

Nine entities. All primary keys are UUID ([ADR 0002](decisions/0002-uuid-primary-keys.md));
all money is integer minor units ([ADR 0001](decisions/0001-money-as-integer-minor-units.md));
all status vocabularies are VARCHAR + CHECK ([ADR 0003](decisions/0003-status-columns-as-varchar-with-check-constraints.md)).

```
merchants
  └── customers ──────────┐
  └── orders ─────────────┤
        └── payment_attempts
  └── events (leak graph spine)
  └── revenue_risks ──────┘
        └── recovery_cases
              ├── recovery_actions
              └── audit_events
```

| Table | Role | Mutability |
|---|---|---|
| `merchants` | Tenant root | mutable |
| `customers` | Party; carries policy state (`contactable`, `contact_count`) | mutable |
| `orders` | The revenue opportunity | mutable |
| `payment_attempts` | Atom of failure analysis; `attempt_number` drives Scenario A | mutable |
| `events` | Append-only spine of the Revenue Leak Graph | **append-only** |
| `revenue_risks` | Deterministic detection output | mutable |
| `recovery_cases` | One recovery attempt per risk | mutable |
| `recovery_actions` | One bounded operation within a case | mutable |
| `audit_events` | Decision and action record | **append-only** |

### Properties the schema guarantees

**Timeline reconstruction survives bad delivery.** `events` carries both
`occurred_at` (when it happened) and `received_at` (when we saw it). Timelines
are always ordered by `occurred_at`, never by insertion order, so delayed and
out-of-order webhooks reconstruct correctly.

**Duplicate delivery cannot double-count revenue.**
`UNIQUE(merchant_id, external_event_id)` on `events` makes ingestion idempotent
at the storage layer, not merely in application logic.

**Money is exact.** Every monetary column is `BigInteger` minor units. A test
asserts no `Float` or `Numeric` column exists anywhere in the schema.
Confidence is `confidence_bps`, an integer 0–10000, so policy thresholds
compare exactly.

**Audit trails cannot be rewritten.** `events` and `audit_events` have
`created_at` but deliberately no `updated_at`, and no update path exists.

### The authority boundary, enforced in the database

Three CHECK constraints make the architecture rule structurally unbreakable —
they hold regardless of what any caller, including a future agent, attempts:

| Constraint | Table | Guarantees |
|---|---|---|
| `ck_recovery_actions_executed_requires_approved` | `recovery_actions` | `executed` implies `approved` |
| `ck_recovery_cases_execution_requires_policy_approval` | `recovery_cases` | no executing/executed/verified state without `policy_status = 'approved'` |
| `ck_audit_events_execution_actor_never_ai` | `audit_events` | an `ai_agent` actor can never appear on an execution entry |

`PolicyStatus` has no "overridden" value. Silent override is not representable.

### Layer status after Phase 1

`app/engine/`, `app/agents/`, `app/services/`, `app/repositories/`, and
`app/integrations/` remain **empty**. Phase 1 added no AI code and no money
math beyond `app/core/money.py`, which is deterministic, network-free, and
LLM-free by construction.

## Simulator (Phase 2)

Deterministic synthetic event generator at `simulator/`, installed editable into
the backend venv. Zero third-party dependencies; standard library only.

```
scenario definition
      ↓
DeterministicRng ── derive("entities"/"timing"/"amounts"/"delivery")
      ↓
SimulationClock (fixed epoch + integer offsets, UTC only)
      ↓
entity generation      merchants → customers → orders → payment_attempts
      ↓
canonical event generation   (causally ordered by occurred_at)
      ↓
delivery transform layer     duplicate / delay / reorder / drop
      ↓
SimulationResult (in-memory)
      ↓
   ┌──┴────────────────────────┐
   ↓                           ↓
fixture.json               [Phase 3] ingestion → PostgreSQL
events.jsonl
frontend.json
```

**Generation is separate from persistence** ([ADR 0004](decisions/0004-simulator-emits-event-stream-not-database-writes.md)).
`simulate(scenario, seed=...)` is pure — no database, no network, no filesystem.
The simulator never writes to `revtrace_dev`; the Phase 3 ingestion layer does.

**Causal order and delivery order are separate.** The delivery transform layer
corrupts arrival without touching `occurred_at`, which is what makes "the
timeline reconstructs correctly despite pathological delivery" testable.

**Duplicates are emitted, not deduplicated.** Suppression is the job of
`UNIQUE(merchant_id, external_event_id)`. A simulator that deduplicated would
make the duplicate-webhook scenario test nothing.

**The simulator generates only merchants, customers, orders, and payment
attempts** — never `revenue_risks`, `recovery_cases`, `recovery_actions`, or
`audit_events` ([ADR 0005](decisions/0005-simulator-does-not-generate-recovery-or-risk-entities.md)).
Recovery scenarios emit `recovery.*` events as historical facts; no approval,
policy decision, or execution authorization is ever fabricated. Ground truth
states what should be detected, never a score, confidence, or recommendation.

**Ground truth lives outside event payloads**, so a detector cannot read the
answer out of its own input. Enforced by test.

17 scenarios across five categories. Fixture format documented in
[docs/contracts/simulation-fixture.md](contracts/simulation-fixture.md).

## Sections to be written

- [x] Simulator design and scenario catalogue (Phase 2) — see above
- [ ] Detection rules and thresholds (Phase 3)
- [ ] Revenue Leak Graph construction and timeline reconstruction (Phase 4)
- [ ] AI evidence contract and structured output schemas (Phase 5)
- [ ] Counterfactual recovery engine and scoring formulas (Phase 6)
- [ ] Policy engine rules and escalation paths (Phase 7)
- [ ] Razorpay adapter boundary (Phase 8)
- [ ] Execution and webhook verification, idempotency strategy (Phase 9)
- [ ] Frontend view model (Phase 10)
- [ ] Evaluation methodology and held-out set (Phase 11)
- [ ] Failure injection matrix and observed behaviour (Phase 12)
