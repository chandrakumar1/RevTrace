# RevTrace — Architecture

**Status: detection, the incrementality ledger, the policy gate, the advisory
hypothesis agent, the Razorpay adapter boundary, webhook verification and the
offline demo are built.** What is *not* built is a live provider integration —
no real Razorpay transaction has ever been processed, and the adapter is
exercised against a deterministic synthetic offline provider. Sections are added
as each component is actually built.

The original specification is [../CLAUDE.md](../CLAUDE.md); current work follows
the Incrementality Ledger closeout plan, which supersedes its phase roadmap.
This document records what was *actually built* and why it diverged, if it did.

---

## Shape

A clean modular monolith. No microservices.

- `backend/` — Python 3.13, FastAPI, SQLAlchemy, Pydantic, PostgreSQL, Alembic
- `frontend/` — React 19, Vite 7, TypeScript, Tailwind 4. No charting library:
  the visualisations are laid out with CSS from the report payload, and every
  visible number comes from the backend through a formatter. Pages 1 and 2 are
  fixture-driven; page 3 — the live demo — calls the backend over a
  development-only Vite proxy at `/api`.
- `simulator/` — synthetic event generator, dev/eval only (Phase 2)

## Core pipeline

What actually runs today, end to end:

```
SIMULATOR                 potential outcomes generated; truth_* held aside
  -> MATERIALISE          tests/benchmark/bridge.py writes the population
  -> ASSIGNMENT           sha256(risk_id : experiment_id : salt) -> arm
  -> OUTCOMES             one case_outcomes row per unit, ITT
  -> ITT ESTIMATORS       rate effect + amount ledger, percentile bootstrap
  -> UPLIFT               5-fold cross-fit, cell-rate T-learner
  -> QINI / QUADRANTS     ranking, capture, quadrant labels
  -> REPORTING            app/reporting/evaluation.py — sole truth reader
  -> PERSISTENCE          uplift_scores, experiment_results
```

The detection pipeline below feeds this: it is what produces the
`revenue_risks` rows that assignment randomises over.

The specification's original pipeline — `AI DIAGNOSIS -> INTERVENTION
SIMULATION -> POLICY GATE -> EXECUTION -> VERIFICATION -> AUDIT` — is now partly
built and partly still intent. The policy gate (`engine/policy_engine.py`),
advisory diagnosis (`agents/hypothesis_agent.py`, checked by
`engine/falsification.py`) and verification (`services/verification/`) exist.
**Live execution against a payment provider does not**: the adapter runs only
against a synthetic offline provider, and no HTTP endpoint moves money.

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

The same rule governs the analytical layer, where it is easier to break and
matters just as much. **No floating-point arithmetic exists anywhere in
`app/causal/` or `app/reporting/`.** Every rate, effect, confidence bound, Qini
coefficient and decomposition component is an integer against a shared
`BPS_SCALE = 10_000`; divisions round halves away from zero through an explicit
helper rather than through `round()`, whose banker's rounding would bias a long
run of ties. A reported figure that cannot be recomputed exactly from stored
integers is not reportable ([ADR 0001](decisions/0001-money-as-integer-minor-units.md)).

The single unavoidable exception is the Gaussian in `app/causal/normal.py`:
there is no float-free route to it in the standard library, so the values are a
stored lookup table, chosen precisely so no float appears anywhere in their
provenance.

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
`app/integrations/` were **empty** at the end of Phase 1. Phase 1 added no AI
code and no money math beyond `app/core/money.py`, which is deterministic,
network-free, and LLM-free by construction. Phase 3 populated `engine/`,
`services/`, and `repositories/`; `agents/` and `integrations/` are still empty.

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

## Detection (Phase 3)

The deterministic detection layer. **No LLM, no Razorpay SDK, no external API
call, and no new third-party dependency** — Phase 3 added zero.

```
POST /api/v1/ingest/simulation
      ↓
ingestion service ────── entities upserted, events appended, duplicates
      ↓                  declined by UNIQUE(merchant_id, external_event_id)
   events (append-only)
      ↓
reconstruction ───────── deduplicate → causal order → state machine →
      ↓                  attempt ledger → integrity flags → gap inference
MerchantTimeline (orders, subscriptions)
      ↓
   ┌──┴──────────────────────┐
   ↓                         ↓
risk_engine (money)     scoring (confidence_bps)
   └──┬──────────────────────┘
      ↓
   detectors ───────────── four pure functions, (timeline, as_of, config)
      ↓
   resolution ──────────── reconcile findings against stored risks
      ↓
   DetectionDelta ──────── new / unchanged / resolved
      ↓
risk_repository ────────── revenue_risks, and nothing else
      ↓
   GET /api/v1/risks, /evidence, /orders/{id}/timeline
```

Everything between "load events" and "persist the delta" is **pure**: no
session, no clock, no network. `app/services/detection/service.py` is the single
seam where the engine meets the database, and it contains no detection logic of
its own.

### The pure core

`as_of` is **injected, never read from a clock** — at the detector, at
resolution, and at the API, where `DetectionRunRequest.as_of` is required and
has no default. Replaying the same timeline at the same `as_of` always produces
identical findings, which is what makes a run auditable.

Configuration is passed explicitly as a `DetectorConfig` rather than read from
module state, so a run's parameters are always visible in its inputs. Every
threshold is an integer — a threshold that gates money must compare exactly.

### Reconstruction: causal order versus delivery order

The Phase 2 delivery transform layer corrupts arrival without touching
`occurred_at`; reconstruction is the other half of that experiment.

| Pathology | Scenario | How it is handled |
|---|---|---|
| Duplicate delivery | S07 | `UNIQUE(merchant_id, external_event_id)` at ingestion; `deduplicate()` for in-memory streams |
| Out-of-order delivery | S08 | Sorted by `occurred_at`; arrival rank preserved separately as `delivery_position` |
| Delayed delivery | S09 | Same — a late event simply sorts into its true place |
| Missing event | S12b | The attempt is **inferred** from later evidence, flagged `inferred: true`, and confidence is penalised |

`received_at` is retained for forensics and **never** used as a detection input.

Order state is derived from events, never read from `orders.status`. Terminal
states are sticky and ranked: `created` < `attempted` < `abandoned`/`cancelled`
< `paid` < `refunded`.

### The money rule that matters

    Amount at risk is the ORDER amount, counted ONCE.

Three failed attempts on one ₹2,304 order put ₹2,304 at risk, not ₹6,912 — three
tries at collecting one debt, not three debts. Summing the attempt ledger is the
natural mistake and is explicitly tested against.

Subscriptions are the deliberate exception: `amount_at_risk` **sums** the failed
cycles, because each failed cycle is a separate charge that never happened.

Currency is verified consistent across an order's attempts and raises rather
than silently picking one. Combining amounts in different currencies is not a
rounding problem; it is a wrong answer, and it should stop the calculation.

### Detectors

Four pure functions. Each was built with its negative controls in the same step.

| Rule | Fires when | Amount at risk | Suppressed by |
|---|---|---|---|
| `repeated_payment_failure.v1` | ≥2 failed attempts clustered within 24h, never collected | order amount, once | any terminal success, **including one that occurred after the failures** |
| `checkout_abandonment.v1` | checkout started, **zero** payment attempts, explicit `checkout.abandoned` or ≥30 min silence before `as_of` | order amount | any attempt at all (that is the repeated-failure detector's case), or a paid order |
| `subscription_payment_failure.v1` | ≥2 **trailing** consecutive failed cycles | sum of failed cycles | a later successful charge resets the streak |
| `reconciliation_mismatch.v1` | captured, but no `order.paid`, past a 1h grace period | **zero** — the money arrived | `order.paid` present, or a refund (a deliberate reversal, not a bookkeeping failure) |

`payment_degradation` remains in the `RiskType` vocabulary with **no detector**.
Deferred by approved decision until a properly designed positive simulator
scenario exists — a baseline-comparison detector with nothing to detect would be
untestable, and shipping one that never fires would be worse than shipping none.

The suppression rules are the substantive half. `reached_terminal_success` is
checked against the whole causal timeline, which is what separates this from a
system that reports every customer who ever had a card declined.

### Resolution: a detector that never retracts is wrong

Fire-and-forget leaves a permanently wrong open risk the moment a late capture
arrives. Each run reconciles current findings against stored ones and produces a
`DetectionDelta` of new / unchanged / resolved. Terminal risks are never
silently reopened; `detected_at` is never overwritten, because moving it would
erase how long a risk has been open and break expiry.

`recovered` means **money arrived, verified from the timeline**. It is never an
assertion that a recovery action was approved or executed. Full table in the
[API contract](contracts/detection-api.md#resolution-semantics).

### Confidence

`confidence_bps` is integer basis points and an explicitly **synthetic/demo
heuristic** — a reproducible measure of evidence strength, never a calibrated
probability ([formula table](contracts/detection-api.md#confidence-is-not-a-probability)).
Every response carrying it also carries
`confidence_is_synthetic_heuristic: true`, so a consumer cannot render it as a
probability by accident. Order amount deliberately does not appear in the score:
a high-value order is not stronger *evidence*. Prioritisation is the amount's
job.

### Persistence

Detection writes to **`revenue_risks` and nothing else**. `risk_repository` has
no import path to `recovery_cases`, `recovery_actions`, or `audit_events`, and
integration tests assert all three stay at zero rows through every scenario.
Identifying a risk is not authorising a response to it.

Identity is the application-level natural key `(merchant_id, order_id,
risk_type)`, upserted rather than re-inserted. `order_id` is nullable —
subscription risks carry none — so lookups use `IS NULL`.

**Known limitation:** there is no unique index behind that key yet. Two
concurrent detection runs for the same merchant could both miss an existing row
and insert duplicates. Detection is single-process today, which makes it
acceptable for Phase 3; a partial unique index is the obvious hardening when
concurrency arrives.

### API

Six feature routes under `/api/v1`, health probes unversioned at the root.
Response contract, real captured examples, and TypeScript types:
[docs/contracts/detection-api.md](contracts/detection-api.md).

**The API is unauthenticated.** No key, no token, no session, no tenant
isolation — `merchant_id` is a filter parameter, not a boundary. This is a
documented Phase 3 gap: authentication was outside the approved scope. Bind to
localhost; do not deploy as-is. What no caller can do, authenticated or not, is
move money — there is no endpoint that approves, executes, refunds, retries,
contacts a customer, or calls Razorpay.

### Ground truth cannot reach detection

Three independent layers, each tested:

1. **The ingestion schema rejects it** — `ground_truth` and nine related keys,
   plus `extra="forbid"` everywhere.
2. **Nothing under `app/` imports the evaluation harness or the simulator** —
   verified by an AST scan over every `app/**/*.py`.
3. **No detector signature accepts it** — verified by `inspect.signature`.

### Truth isolation, stated for the whole system

Ground truth lives in `case_outcomes.truth_*` — the potential outcomes the
generator planted, which no real system could observe. The rule is narrower than
"detection cannot see it":

**`app/reporting/evaluation.py` is the sole module in `app/` permitted to read a
`truth_*` column.** Scoring an estimator against the answer key is exactly what
an evaluation is for, and confining that to one module is what keeps every other
number non-circular. Everywhere else the rule is absolute: `app/causal/` may not
so much as *name* a truth column, verified by an AST scan that walks every
`Name`, `Attribute` and `Constant` node in the package. If the estimator could
read the answer, its accuracy would be a tautology rather than a measurement.

The Phase 3 guard has a second, load-bearing consequence. It rejects any `app/`
module importing a module whose name contains `evaluation` — which means **no
application module may import the reporting layer at all**, including the
repositories that persist its output.

That is why persistence crosses the boundary by **DTO rather than by object**.
`experiment_result_repository.persist_result` takes a flat frozen `ResultValues`
carrying only integers and a nullable Qini coefficient; it never imports the
report type, and the mapping from report to DTO lives in the test tree on the
other side of the boundary. The alternative — an adapter inside `app/` — is not
available, because such an adapter would have to import the reporting module and
would trip the same guard. The awkwardness is the guard working, not a design
accident.

### Evaluation — synthetic/demo measurement

The harness lives in `tests/evaluation/` precisely so it cannot be imported by
`app/`. Hermetic: it simulates, reconstructs, and detects in memory. Metrics are
integer basis points, never floats.

Across all 17 Phase 2 scenarios at `seed=42`:

| | |
|---|---|
| True positives | 10 |
| False positives | **0** |
| False negatives | 0 |
| Amount mismatches | 0 |
| Precision | 10000 bps |
| Recall | 10000 bps |

**These are synthetic/demo measurements over generated data.** They say the
detectors behave correctly on the 17 scenarios that exist. They are not evidence
of real-world accuracy, they are not a held-out evaluation set, and they must
never be reported without that label. A held-out set is Phase 11's job.

Seven scenarios (S01, S02, S03, S07, S11, S13, S14) are **negative controls**
producing zero findings. They are what make the precision figure meaningful
rather than trivial: evaluated alone they yield `precision = null` — nothing was
detected, so there was nothing to be wrong about — which is reported honestly
rather than as a fabricated 100%.

17 committed JSON regression fixtures snapshot every detector's output, so a
behaviour change fails loudly instead of drifting.

### Phase 4 overlap, stated plainly

Timeline reconstruction was scheduled for Phase 4 but had to be built in Phase 3
— a detector cannot run without a reconstructed timeline. `app/services/tracing/`
and `GET /api/v1/orders/{order_id}/timeline` therefore already exist. Phase 4's
remaining scope is the cross-order, cross-customer **graph** view rather than
the per-order timeline.

## Incrementality ledger

The layer that lets RevTrace claim causation rather than correlation. It reads
`revenue_risks` produced by detection and answers a question detection cannot:
of the money that arrived, how much did we cause?

Pre-registered in [EXPERIMENT_DESIGN.md](EXPERIMENT_DESIGN.md). No LLM, no
provider call, no new third-party dependency.

### Assignment is deterministic, never drawn

An arm is a pure function of stored inputs:

```
bucket = int(sha256(f"{risk_id}:{experiment_id}:{salt}")[:8], 16) % 10000
```

Nothing is sampled at runtime, so an auditor can recompute any unit's arm years
later from the row alone. The salt is configuration, deliberately not a secret
and deliberately not generated per run; moving it re-randomises every assignment
in every experiment, which voids a running experiment rather than tuning it.

**Intention-to-treat, exactly as stored.** The arm comes from `case_assignments`
and is never re-derived. A unit whose execution failed is a treated unit with a
disappointing outcome; moving it to control would let the treatment look best
precisely where it worked least. The denominator is fixed at randomisation.

### The two-level cell ladder

Uplift is estimated per cell rather than per unit, and cells resolve down a
ladder, finest first ([`app/causal/cells.py`](../backend/app/causal/cells.py)):

| Level | Key | Constant |
|---|---|---|
| 0 — fine | `failure_code \| payment_method` | `FINE` |
| 1 — coarse | `failure_code` | `COARSE` |

A unit is scored at the first level whose cell **qualifies** — the exact
unequal-allocation power requirement at that cell's own arm ratio, with empty
arms and impossible baselines refused first. A thin fine cell backs off to the
coarse cell rather than losing its label.

A unit that qualifies at neither level falls to the global training rates and is
marked **non-qualifying** (`GLOBAL_CELL`, `GLOBAL_FALLBACK`). This is the
important part of the design: a global average is not a conditional estimate,
and the fallback exists so that fact is recorded rather than disguised. A
non-qualifying unit still has a number; it does not have a label.

`amount_band`, `hour_bucket` and `tenure_bucket` are carried as features but are
deliberately **not** ladder keys. At benchmark size a three-way cell holds
roughly fifty units per arm against a floor near four hundred, so every such
cell would fail qualification and every unit would end up unlabelled. They are
named and tested so a larger run can promote them without inventing a
representation at that point.

### Cross-fitting

`DEFAULT_FOLD_COUNT = 5`. A unit is scored by a model built from the four folds
it is **not** in, so no prediction is influenced by the outcome it is
predicting. Fitting and scoring on the same rows would make uplift, ranking and
Qini optimistic by an amount nobody could measure afterwards.

Fold membership is assigned the same way arms are — a deterministic hash, never
a draw — under `FOLD_SALT`, which is distinct from the assignment salt so that
fold and arm are independent rather than two views of one hash. A cell is
bootstrapped once per fold and reused across every unit that lands in it, which
keeps cost proportional to the number of cells rather than to the number of
units.

The model itself is a **T-learner over empirical cell rates**: two lookup tables
of integer basis points, one per arm, differenced. No fitting, no coefficients,
no dependency. The features are already categorical, so a rate per cell is not a
simplification of a model — it *is* the model, with the useful property that
every prediction traces back to a count a reader can check.

### Reporting and persistence

`app/reporting/evaluation.py` assembles the report and is the only module
permitted to compare an estimate against the answer key (see *Truth isolation*
above). Two structural pieces of it are worth naming here:

- **Interval calibration** is exercised by a fixed multi-seed coverage sweep
  ([`tests/benchmark/coverage.py`](../backend/tests/benchmark/coverage.py)),
  which materialises independent populations and counts how often the interval
  contains the planted effect. The seeds are a contiguous block fixed before any
  result was seen, and the sweep asserts no particular coverage number — picking
  seeds after seeing coverage is how a calibration study lies.
- **The rate/mix decomposition** splits the incremental figure into a
  recovery-rate effect, priced at the holdout's mean order value, and a residual
  attributed to order mix. The two sum to the incremental figure exactly by
  construction, because the second is defined as the residual. It is an
  accounting identity, not a causal decomposition, and there is deliberately no
  third interaction term.

Results are quantitative and belong to the generated evaluation, not to this
document. `docs/EVALUATION.md` is the authoritative source for every figure;
repeating one here would create a second copy that drifts.

Persistence writes `uplift_scores` and `experiment_results` through repositories
that validate every row before adding any, flush once, and never commit — the
caller owns the transaction. Neither imports the reporting layer; see the DTO
boundary above.

### The acceptance run

[`backend/run_acceptance.py`](../backend/run_acceptance.py) is the only
non-test entry point. It materialises the population, evaluates it **with the
uplift model fitted**, and writes `docs/EVALUATION.md` and
`docs/evaluation.json` inside a transaction it always rolls back.

`build_report` omits the uplift model by default, so the runner requests it
explicitly and then refuses to write unless `report.uplift` is actually present.
Without that guard a report missing its model would overwrite the artifact with
a strictly smaller document — deleting the quadrant, Qini, confusion-matrix and
uplift-limitation sections while looking like a refresh.

## Sections to be written

- [x] Simulator design and scenario catalogue — see above
- [x] Detection rules and thresholds — see above
- [x] Timeline reconstruction — see above
- [x] Incrementality ledger: assignment, cell ladder, cross-fitting, reporting
      and persistence — see above
- [ ] Frontend view model, and wiring it to live output rather than fixtures
- [ ] Counterfactual recovery engine and scoring formulas
- [ ] Policy engine rules and escalation paths
- [ ] AI evidence contract and structured output schemas
- [ ] Razorpay adapter boundary
- [ ] Execution and webhook verification, idempotency strategy
- [ ] Failure injection matrix and observed behaviour
- [ ] Revenue Leak Graph construction across orders and customers
