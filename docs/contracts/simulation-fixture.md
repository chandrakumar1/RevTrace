# Simulation fixture contract

The contract between the Phase 2 simulator and its consumers: the Phase 3
ingestion layer, and the frontend dashboard.

**Stability:** the shapes below are the ones to build against. `revenue_risk`
and `recovery_state` are present and explicitly `null` in Phase 2 so the UI can
be written now against their final position.

---

## Producing a fixture

```bash
cd backend
.venv/bin/python -m simulator generate S04 --seed 42
```

Writes three files into `simulator/output/S04_seed42/`:

| File | Consumer | Purpose |
|---|---|---|
| `fixture.json` | Phase 3, evaluation | Canonical, complete, checksummed |
| `events.jsonl` | Ingestion | Delivery stream, one delivery per line |
| `frontend.json` | Dashboard | Flattened read-optimized view-model |

A committed sample lives at
[`frontend/src/fixtures/sample_S04_seed42.json`](../../frontend/src/fixtures/sample_S04_seed42.json).

## Universal rules

- **Money is always an integer count of minor units** (paise for INR). Keys
  ending in `_minor` hold money. Never a float, never a formatted string.
  Formatting for display is the consumer's job.
- **Timestamps are ISO-8601 UTC with a trailing `Z`.** Never naive, never local.
- **`occurred_at` is causal truth; `received_at` is arrival.** Always
  `received_at >= occurred_at`. Reconstruct timelines by `occurred_at`, never by
  arrival order.
- **Ground truth is never in an event payload.** It lives only in
  `fixture.json`'s `ground_truth` section, so detection cannot read the answer
  from its own input.

---

## `fixture.json`

```jsonc
{
  "manifest":     { /* reproduction metadata */ },
  "entities":     { /* merchants, customers, orders, payment_attempts */ },
  "deliveries":   [ /* delivery-ordered; MAY contain duplicates */ ],
  "ground_truth": { /* expectations — Phase 3 / Phase 11 only */ }
}
```

### `manifest`

| Field | Type | Notes |
|---|---|---|
| `scenario_id` | string | e.g. `"S04"` |
| `scenario_name` | string | e.g. `"repeated_payment_failure"` |
| `category` | string | `baseline` / `leak` / `delivery_integrity` / `reconciliation` / `recovery` |
| `seed` | integer | Non-negative |
| `generator_version` | string | Bumped when generation logic changes output |
| `epoch` | ISO-8601 | Fixed simulation anchor |
| `currency` | string | ISO 4217, 3 letters |
| `counts` | object | `merchants`, `customers`, `orders`, `payment_attempts`, `events_emitted`, `events_unique` |
| `window_start` / `window_end` | ISO-8601 | Earliest / latest `occurred_at` |
| `checksum` | string | `sha256:<hex>` over entities + deliveries + ground truth |

Regenerating with the same `(scenario_id, seed, generator_version)` reproduces
the identical checksum. A mismatch with an unchanged `generator_version` means
something regressed.

### `entities`

Each entity maps directly onto its Phase 1 table.

```jsonc
"orders": [{
  "id": "uuid", "merchant_id": "uuid", "customer_id": "uuid|null",
  "external_order_id": "sim_order_42_1",
  "amount": 230400, "currency": "INR", "status": "attempted"
}],
"payment_attempts": [{
  "id": "uuid", "order_id": "uuid", "customer_id": "uuid|null",
  "external_payment_id": "sim_pay_42_1",
  "amount": 230400, "currency": "INR",
  "payment_method": "card", "provider": "simulator", "status": "failed",
  "failure_code": "card_declined",
  "failure_reason": "Card declined by the issuing bank",
  "attempt_number": 1, "attempted_at": "2026-01-01T00:00:30Z"
}]
```

`provider` is always `"simulator"`, never `"razorpay"` — nothing here came from
Razorpay and the data must not claim otherwise.

### `deliveries`

```jsonc
{
  "envelope": {
    "sequence": 1,            // 1-based arrival position
    "delivery_attempt": 1,    // 2+ means a redelivery
    "is_duplicate": false,
    "is_delayed": false,
    "delay_seconds": 0,
    "is_out_of_order": false  // arrived after a later-occurring event
  },
  "event": {
    "id": "uuid",
    "merchant_id": "uuid",
    "customer_id": "uuid|null",
    "order_id": "uuid|null",
    "external_event_id": "sim_evt_S04_42_000001",
    "event_type": "payment.failed",
    "payload": { /* provider-neutral */ },
    "occurred_at": "2026-01-01T00:00:33Z",
    "received_at": "2026-01-01T00:00:35Z"
  }
}
```

**The envelope is delivery bookkeeping and is not part of the `events` row.**
Only `event` maps to the schema. Its keys are exactly the Phase 1 columns.

**Duplicates share one `external_event_id`** and differ only in `received_at`.
`external_event_id` is the idempotency key backing
`UNIQUE(merchant_id, external_event_id)`; ingestion is expected to reject the
second insert.

### `ground_truth`

**Phase 3 and Phase 11 only. Never render this in the UI, and never feed it to a
detector as input.**

| Field | Notes |
|---|---|
| `expected_risks` | `risk_type` (a Phase 1 `RiskType`), `amount_at_risk` (minor units), `currency`, `order_ref`, `reason` |
| `expected_anomalies` | `anomaly_kind`, `order_ref`, `reason` — for inconsistencies with no `RiskType` |
| `emitted_event_count` | Deliveries emitted, duplicates included |
| `expected_persisted_event_count` | Rows expected after idempotent ingestion |
| `dropped_events` | `external_event_id`s generated but never delivered |
| `duplicated_events` | `external_event_id`s delivered more than once |
| `narrative` | Plain-language description of the scenario |

There is deliberately **no** confidence, score, recommendation, or
expected-recovery figure. Those belong to the deterministic engines in Phases 3
and 6 (see [ADR 0005](../decisions/0005-simulator-does-not-generate-recovery-or-risk-entities.md)).

---

## `events.jsonl`

One JSON object per line, in arrival order:

```
{"delivery":{...},"event":{...}}
```

Feed it to ingestion in file order to replay delivery exactly as it happened,
duplicates and all.

---

## `frontend.json` — the dashboard contract

```jsonc
{
  "summary": {
    "scenario_id": "S04",
    "scenario_name": "repeated_payment_failure",
    "merchant_name": "Synthetic Merchant 1",
    "currency": "INR",
    "orders_total": 1,
    "customers_total": 1,
    "amount_captured_minor": 0,
    "amount_at_risk_minor": 230400,
    "events_total": 7,
    "events_unique": 7
  },
  "orders": [
    { "order_ref": "sim_order_42_1", "amount_minor": 230400,
      "currency": "INR", "status": "attempted" }
  ],
  "timeline": [
    { "sequence": 1,
      "occurred_at": "2026-01-01T00:00:00Z",
      "received_at": "2026-01-01T00:00:02Z",
      "event_type": "order.created",
      "external_event_id": "sim_evt_S04_42_000001",
      "order_ref": "sim_order_42_1",
      "is_duplicate": false,
      "is_out_of_order": false,
      "is_delayed": false,
      "delay_seconds": 0,
      "summary": "Order created" }
  ],
  "payment_attempts": [
    { "attempt_number": 1, "payment_ref": "sim_pay_42_1",
      "status": "failed", "method": "card",
      "amount_minor": 230400, "currency": "INR",
      "failure_code": "card_declined",
      "failure_reason": "Card declined by the issuing bank",
      "attempted_at": "2026-01-01T00:00:30Z" }
  ],
  "revenue_risk": null,
  "recovery_state": null
}
```

### Notes for the UI

- `timeline` is in **arrival order** and includes duplicates. Render
  `is_duplicate`, `is_delayed`, and `is_out_of_order` as badges — surfacing
  delivery pathology is a feature, not noise. To show the *causal* timeline
  instead, sort by `occurred_at` and drop repeated `external_event_id`s.
- `payment_attempts` is sorted by `attempted_at`.
- `summary.amount_at_risk_minor` is derived from ground truth in Phase 2. From
  Phase 3 it will come from the deterministic risk engine instead. The field
  position does not change.
- `revenue_risk` is filled by Phase 3; `recovery_state` by Phases 6–9. Both are
  `null` now — build the components, leave them empty.
- Every metric derived from this data is a **synthetic/demo** measurement and
  must be labelled as such in the UI itself, not only in documentation.

### Suggested TypeScript shape

```ts
type MinorUnits = number;   // integer; never a float

interface TimelineEntry {
  sequence: number;
  occurred_at: string;      // ISO-8601 UTC
  received_at: string;
  event_type: string;
  external_event_id: string;
  order_ref: string | null;
  is_duplicate: boolean;
  is_out_of_order: boolean;
  is_delayed: boolean;
  delay_seconds: number;
  summary: string;
}

interface SimulationFixture {
  summary: {
    scenario_id: string;
    scenario_name: string;
    merchant_name: string | null;
    currency: string;
    orders_total: number;
    customers_total: number;
    amount_captured_minor: MinorUnits;
    amount_at_risk_minor: MinorUnits;
    events_total: number;
    events_unique: number;
  };
  orders: Array<{
    order_ref: string;
    amount_minor: MinorUnits;
    currency: string;
    status: string;
  }>;
  timeline: TimelineEntry[];
  payment_attempts: Array<{
    attempt_number: number;
    payment_ref: string;
    status: string;
    method: string;
    amount_minor: MinorUnits;
    currency: string;
    failure_code: string | null;
    failure_reason: string | null;
    attempted_at: string;
  }>;
  revenue_risk: null;       // Phase 3
  recovery_state: null;     // Phases 6-9
}
```
