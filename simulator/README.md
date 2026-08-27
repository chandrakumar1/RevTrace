# RevTrace Simulator

Deterministic synthetic event generator for development and evaluation.

**Status: Phase 2 complete.** 17 scenarios, zero third-party dependencies, no
database, no network.

## Why this is a top-level directory

The simulator is a development and evaluation tool, not a runtime service. It
lives outside `backend/app/` so that test-data generation can never be imported
by production code paths. It reuses two backend modules — `app.models.enums` for
the controlled vocabularies and `app.core.money` for minor-unit arithmetic — so
that vocabulary and money logic have a single source of truth. The backend never
depends on the simulator; a test enforces the direction.

## Setup

Installed editable into the backend venv:

```bash
cd backend
.venv/bin/pip install -e ../simulator
```

## Usage

```bash
cd backend

.venv/bin/python -m simulator list
.venv/bin/python -m simulator generate S04 --seed 42
.venv/bin/python -m simulator generate S04 --seed 42 --dry-run --checksum-only
.venv/bin/python -m simulator generate-set baseline+leak --seed 7
.venv/bin/python -m simulator inspect simulator/output/S04_seed42 --show-timeline
.venv/bin/python -m simulator verify simulator/output/S04_seed42
```

As a library:

```python
from simulator import simulate

result = simulate("S04", seed=42)
result.manifest.checksum  # reproducible
result.deliveries  # arrival order, may contain duplicates
result.events_in_causal_order  # unique, sorted by occurred_at
result.ground_truth  # Phase 3 / Phase 11 only
```

`simulate()` is pure: no I/O, no database, no network.

## Determinism contract

`(scenario, seed, params, generator_version)` always produces byte-identical
output, verified by the SHA-256 checksum in the manifest.

This holds because:

- **All randomness is seeded.** `random.Random(seed)` instances, never the
  module-level API. `random` is imported by `rng.py` and nowhere else.
- **UUIDs are derived from the seed**, not `uuid.uuid4()` — which draws from
  `os.urandom` and would differ every run.
- **Time is a fixed epoch plus integer offsets.** `datetime.now()` is never
  called.
- **Money is drawn as integer minor units.** No float ever enters the chain.
- **Sub-streams are isolated.** Each concern (`entities`, `timing`, `amounts`,
  `delivery`) gets its own generator derived from `sha256(seed, label)`, so
  adding a draw in one concern cannot shift values in another.

`tests/simulator/test_no_nondeterminism.py` parses the AST of every source file
and fails the build if a banned call reappears.

## Causal order vs delivery order

Two independent orderings, always distinguishable:

- **Causal** — `occurred_at`. This is truth. Timelines are always rebuilt this
  way.
- **Delivery** — `envelope.sequence`, tracking `received_at`. This is what
  arrived, in the order it arrived.

Delivery transforms corrupt arrival — duplicating, delaying, reordering,
dropping — without ever touching `occurred_at`. That separation is what makes
"the timeline reconstructs correctly despite pathological delivery" testable.

## Scenario catalog

### Baseline — must produce zero detections

| ID | Name | Purpose |
|---|---|---|
| S01 | `healthy_payment` | The happy path |
| S02 | `failure_then_retry_success` | **False-positive guard** — a recovered failure is not a leak |
| S03 | `multiple_attempts_eventual_success` | Multiple attempts alone are not a leak |
| S14 | `mixed_merchant_baseline` | ~85% success over 20 orders; the historical baseline |

### Revenue leak

| ID | Name | Spec |
|---|---|---|
| S04 | `repeated_payment_failure` | Scenario A |
| S04b | `high_value_repeated_failure` | Scenario A, amount-aware prioritisation |
| S04c | `upi_timeout_failures` | Scenario A, timeout ≠ hard decline |
| S05 | `checkout_abandonment` | Scenario B — absence of events is the signal |
| S06 | `subscription_payment_failure` | Scenario C |
| S13 | `refund_after_capture` | Negative case — a refund is **not** a leak |

### Delivery integrity — same expected outcome as the clean counterpart

| ID | Name | Abnormality |
|---|---|---|
| S07 | `duplicate_webhook_delivery` | Two events redelivered under one `external_event_id` |
| S08 | `out_of_order_delivery` | Full history arrives reversed |
| S09 | `delayed_event_arrival` | One event six hours late |
| S12b | `missing_event` | One event never delivered |

### Reconciliation and recovery

| ID | Name | Notes |
|---|---|---|
| S10 | `payment_captured_order_not_reconciled` | Untyped **anomaly** — no Phase 1 `RiskType` fits |
| S11 | `recovery_success` | Historical `recovery.*` events only |
| S12 | `recovery_failure` | Expected recovery ≠ actual recovery |

**Deferred:** chargebacks (no `EventType` or `PaymentStatus` supports them;
adding either would require a Phase 1 migration) and merchant-wide payment
degradation (Scenario D — better built alongside its Phase 3 detector).

## Idempotency

The simulator **emits** duplicates; storage **rejects** them. It never
deduplicates. `events.jsonl` is a delivery log, and delivery logs legitimately
contain redeliveries — suppression is the job of
`UNIQUE(merchant_id, external_event_id)` in the Phase 1 schema, and
demonstrating that is the point of S07.

## Output

Generated files go to `simulator/output/`, which is gitignored — generated data
is not source. The format is documented in
[`docs/contracts/simulation-fixture.md`](../docs/contracts/simulation-fixture.md).

## What the simulator does not do

It generates synthetic events. Nothing else. It does not detect leaks, score
risk, recommend recovery, make policy decisions, invoke AI, call Razorpay,
execute refunds, or write to any database.

It generates only merchants, customers, orders, and payment attempts — **never**
`revenue_risks`, `recovery_cases`, `recovery_actions`, or `audit_events`. See
[ADR 0005](../docs/decisions/0005-simulator-does-not-generate-recovery-or-risk-entities.md).

## Ground truth

The simulator knows the true cause of every generated case, because it authored
them. That ground truth is what makes real measurement possible in Phase 11 —
true positives, false positives, detection precision.

It is stored in its own section of `fixture.json` and **never in an event
payload**, so a detector cannot read the answer out of its own input. A test
enforces this.

Metrics derived from this data are **synthetic/demo** measurements and must be
labelled as such wherever they are reported. Do not cherry-pick examples.
