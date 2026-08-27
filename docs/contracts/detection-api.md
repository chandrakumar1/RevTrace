# Detection API contract

The contract between the Phase 3 backend and its consumers — principally the
Phase 10 frontend dashboard.

**Stability:** the shapes below are the ones to build against. Every example on
this page is **real captured output** from the implemented endpoints, not an
illustration. They were produced by ingesting scenario `S04` at `seed=42` and
running detection at `as_of = 2026-06-01T00:00:00Z`.

**Status: unauthenticated.** See [Security posture](#security-posture) before
exposing this anywhere but localhost.

---

## Universal rules

Same as the [simulation fixture contract](simulation-fixture.md), and for the
same reasons:

- **Money is an integer count of minor units** (paise for INR), under keys
  ending in `_minor`. Never a float, never a formatted string. Formatting for
  display is the consumer's job.
- **Timestamps are ISO-8601 UTC with a trailing `Z`.** Enforced by a shared
  `UtcDatetime` type on every response field, because PostgreSQL returns
  `timestamptz` in the session timezone and the wire format must not depend on
  where the server happens to be.
- **`occurred_at` is causal truth; `received_at` is arrival.** Always
  `received_at >= occurred_at`. Render timelines by `occurred_at`, never by
  arrival order.
- **`confidence_bps` is an integer 0–10000 and is not a probability.** See
  [Confidence](#confidence-is-not-a-probability).
- **`amount_at_risk_minor` may legitimately be `0`.** See
  [ADR 0007](../decisions/0007-reconciliation-mismatch-risk-type.md).

## Routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness. **Unversioned** — deliberately at the root. |
| `GET` | `/health/db` | Database readiness. Unversioned. |
| `POST` | `/api/v1/ingest/simulation` | Load a simulator fixture |
| `POST` | `/api/v1/detection/runs` | Run detection for one merchant |
| `GET` | `/api/v1/risks` | List detected risks |
| `GET` | `/api/v1/risks/{risk_id}` | One risk |
| `GET` | `/api/v1/risks/{risk_id}/evidence` | Evidence, derived at read time |
| `GET` | `/api/v1/orders/{order_id}/timeline` | Reconstructed causal timeline |

Feature routes are versioned; health probes are not, because a load balancer's
health check should not have to track an API version. OpenAPI is served at
`/openapi.json` with interactive docs at `/docs`.

## Shared TypeScript types

```ts
/** Integer count of minor units (paise for INR). Never a float. */
type MinorUnits = number;

/** ISO-8601 UTC with a trailing Z. */
type UtcTimestamp = string;

/** Integer basis points, 0-10000. NOT a probability — see below. */
type BasisPoints = number;

type RiskType =
  | "repeated_payment_failure"
  | "checkout_abandonment"
  | "subscription_payment_failure"
  | "payment_degradation"          // reserved; no detector in Phase 3
  | "reconciliation_mismatch";

type RiskStatus =
  | "detected"                     // open
  | "under_investigation"          // open
  | "recovery_in_progress"         // open
  | "recovered"                    // terminal
  | "unrecoverable"                // terminal
  | "false_positive"               // terminal
  | "expired";                     // terminal

type MoneyBreakdown = {
  order_amount_minor: MinorUnits;
  captured_minor: MinorUnits;
  failed_minor: MinorUnits;
  refunded_minor: MinorUnits;
  recovered_minor: MinorUnits;
  outstanding_minor: MinorUnits;
};

type Integrity = {
  duplicate_deliveries: number;    // 0 when rebuilt from stored rows — see note
  out_of_order_deliveries: number;
  max_delivery_lag_seconds: number;
  inferred_gaps: number;           // events that never arrived, inferred
};

type Attempt = {
  attempt_number: number;
  payment_ref: string;
  outcome: string;
  payment_method: string | null;
  amount_minor: MinorUnits;
  currency: string | null;
  failure_code: string | null;
  failure_reason: string | null;
  first_seen_at: UtcTimestamp;
  inferred: boolean;               // this attempt's own event never arrived
};
```

---

## `POST /api/v1/ingest/simulation`

Loads a simulator fixture **with its `ground_truth` section removed**. A body
carrying `ground_truth` is rejected — it is evaluation data and must never reach
detection input ([ADR 0006](../decisions/0006-ingestion-accepts-entities-and-events.md)).

Request: `{ entities, deliveries, manifest? }`, exactly the fixture shape minus
the answer key. `manifest` is optional and echoed back for traceability.

**Response `201`:**

```json
{
  "merchants_upserted": 1,
  "customers_upserted": 1,
  "orders_upserted": 1,
  "payment_attempts_upserted": 3,
  "events_received": 7,
  "events_persisted": 7,
  "duplicates_suppressed": 0,
  "scenario_id": "S04",
  "seed": 42
}
```

**Idempotent.** Re-posting the identical body:

```json
{
  "merchants_upserted": 0,
  "customers_upserted": 0,
  "orders_upserted": 0,
  "payment_attempts_upserted": 0,
  "events_received": 7,
  "events_persisted": 0,
  "duplicates_suppressed": 7,
  "scenario_id": "S04",
  "seed": 42
}
```

Duplicates are offered to the database and declined by
`UNIQUE(merchant_id, external_event_id)`, not filtered in application code.
`duplicates_suppressed` is the only place duplicate counts are observed — see
the note under [evidence](#get-apiv1risksrisk_idevidence).

Ingestion is **all-or-nothing**: a single malformed event, or an event
referencing an unknown merchant or order, rejects the whole batch with `422`.
Nothing partial is ever written.

---

## `POST /api/v1/detection/runs`

Reconstructs every timeline for one merchant, runs the deterministic detectors,
reconciles against previously stored risks, and persists the delta.

```ts
type DetectionRunRequest = {
  merchant_id: string;   // UUID
  as_of: UtcTimestamp;   // REQUIRED, no default
};
```

**`as_of` is required and has no default.** A run that read the server clock
would not be reproducible, and reproducibility is the whole basis of the audit
trail. Re-running with the same `as_of` is idempotent.

**Response `200`:**

```json
{
  "merchant_id": "4c415c25-c5fe-4800-9948-fecdcb970084",
  "as_of": "2026-06-01T00:00:00Z",
  "orders_examined": 1,
  "subscriptions_examined": 0,
  "events_examined": 7,
  "risks_created": 1,
  "risks_unchanged": 0,
  "risks_resolved": 0,
  "total_amount_at_risk_minor": 230400,
  "total_recovered_minor": 0,
  "resolutions": []
}
```

`total_amount_at_risk_minor` covers **open risks only**, and is recomputed from
stored rows rather than accumulated across runs.

### A run that resolves a risk

Loading the recovery scenario `S11` over the same merchant — the simulator is
deterministic, so `seed=42` produces the same merchant and order UUIDs, and the
recovery events extend the same order's timeline — then re-running detection:

```json
{
  "merchant_id": "4c415c25-c5fe-4800-9948-fecdcb970084",
  "as_of": "2026-06-01T00:00:00Z",
  "orders_examined": 1,
  "subscriptions_examined": 0,
  "events_examined": 11,
  "risks_created": 0,
  "risks_unchanged": 0,
  "risks_resolved": 1,
  "total_amount_at_risk_minor": 0,
  "total_recovered_minor": 230400,
  "resolutions": [
    {
      "risk_type": "repeated_payment_failure",
      "order_id": "8f52032a-46d2-483e-833e-a34459a9a0e7",
      "previous_status": "detected",
      "new_status": "recovered",
      "reason": "A recovery action was executed and payment was subsequently captured; the revenue was recovered.",
      "amount_recovered_minor": 230400
    }
  ]
}
```

`amount_recovered_minor` is **reported, not stored**: `revenue_risks` has no
column for it, and recovery accounting belongs to Phase 6. Only the risk's
`status` changes in the database. See [Resolution semantics](#resolution-semantics).

---

## `GET /api/v1/risks`

Query parameters: `merchant_id` (UUID), `risk_type`, `status`, `limit`
(1–200, default 50), `offset` (default 0). An unknown `risk_type` or `status`
returns `422` rather than silently matching nothing.

```json
{
  "items": [
    {
      "risk_id": "1ac476ba-8e9a-4038-b03c-d00f25c0ba95",
      "merchant_id": "4c415c25-c5fe-4800-9948-fecdcb970084",
      "order_id": "8f52032a-46d2-483e-833e-a34459a9a0e7",
      "customer_id": "c5af734d-29b1-4625-9d91-550660274ccf",
      "order_ref": "sim_order_42_1",
      "risk_type": "repeated_payment_failure",
      "status": "detected",
      "amount_at_risk_minor": 230400,
      "currency": "INR",
      "confidence_bps": 7000,
      "confidence_is_synthetic_heuristic": true,
      "detection_rule": "repeated_payment_failure.v1",
      "detected_at": "2026-06-01T00:00:00Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

`total` is the count **before** pagination. Ordering is
`detected_at DESC, risk_type` — stable, so pagination does not shuffle.

`order_ref` is the human-readable external reference, resolved in one batched
query. It is `null` for subscription risks, which carry no order.

---

## `GET /api/v1/risks/{risk_id}`

`RiskSummary` plus lifecycle fields. Evidence is **not** inlined — it is derived
at read time and served separately, so a detail fetch stays cheap
([ADR 0008](../decisions/0008-evidence-is-derived-not-stored.md)).

```json
{
  "risk_id": "1ac476ba-8e9a-4038-b03c-d00f25c0ba95",
  "merchant_id": "4c415c25-c5fe-4800-9948-fecdcb970084",
  "order_id": "8f52032a-46d2-483e-833e-a34459a9a0e7",
  "customer_id": "c5af734d-29b1-4625-9d91-550660274ccf",
  "order_ref": "sim_order_42_1",
  "risk_type": "repeated_payment_failure",
  "status": "detected",
  "amount_at_risk_minor": 230400,
  "currency": "INR",
  "confidence_bps": 7000,
  "confidence_is_synthetic_heuristic": true,
  "detection_rule": "repeated_payment_failure.v1",
  "detected_at": "2026-06-01T00:00:00Z",
  "created_at": "2026-08-27T08:09:40.961616Z",
  "updated_at": "2026-08-27T08:09:40.961616Z",
  "is_true_positive": null,
  "evidence_url": "/api/v1/risks/1ac476ba-8e9a-4038-b03c-d00f25c0ba95/evidence"
}
```

`is_true_positive` is `null` and stays `null` in Phase 3. It is Phase 11
evaluation labelling, and detection must not label its own output.

`detected_at` is the supplied `as_of` — when the risk was **first** seen. It is
never overwritten on a re-run, because moving it would erase how long the risk
has been open and break expiry. `updated_at` is the row's last write.

`404` if the risk does not exist: `{"detail": "risk <uuid> not found"}`.

---

## `GET /api/v1/risks/{risk_id}/evidence`

Everything supporting the finding, reconstructed from immutable events on
demand.

```json
{
  "risk_id": "1ac476ba-8e9a-4038-b03c-d00f25c0ba95",
  "risk_type": "repeated_payment_failure",
  "status": "detected",
  "detection_rule": "repeated_payment_failure.v1",
  "order_id": "8f52032a-46d2-483e-833e-a34459a9a0e7",
  "order_ref": "sim_order_42_1",
  "order_state": "attempted",
  "current_reason": "3 failed payment attempts with no successful payment; failure_code=card_declined",
  "contributing_event_ids": [
    "sim_evt_S04_42_000002",
    "sim_evt_S04_42_000003",
    "sim_evt_S04_42_000004",
    "sim_evt_S04_42_000005",
    "sim_evt_S04_42_000006",
    "sim_evt_S04_42_000007"
  ],
  "attempts": [
    {
      "attempt_number": 1,
      "payment_ref": "sim_pay_42_1",
      "outcome": "failed",
      "payment_method": "card",
      "amount_minor": 230400,
      "currency": "INR",
      "failure_code": "card_declined",
      "failure_reason": "Card declined by the issuing bank",
      "first_seen_at": "2026-01-01T00:00:30Z",
      "inferred": false
    }
  ],
  "integrity": {
    "duplicate_deliveries": 0,
    "out_of_order_deliveries": 0,
    "max_delivery_lag_seconds": 5,
    "inferred_gaps": 0
  },
  "money": {
    "order_amount_minor": 230400,
    "captured_minor": 0,
    "failed_minor": 230400,
    "refunded_minor": 0,
    "recovered_minor": 0,
    "outstanding_minor": 230400
  },
  "events_examined": 7
}
```

*(Attempts 2 and 3 elided above; the real response carries all three.)*

Three things a consumer must handle:

**`current_reason` is nullable.** The reason is re-derived, not stored. A
resolved risk no longer fires, so `current_reason` reads `null` and
`contributing_event_ids` is `[]`. That is correct: there is no longer a reason,
and replaying a stale one would misrepresent the risk's state.

**`duplicate_deliveries` reads `0` here, always.** Redeliveries were rejected by
the unique constraint at ingestion, so they never became rows to count. That is
a property of the source, not a defect. Duplicate counts live in the ingestion
response.

**`failed_minor` is `230400`, not `691200`.** Three failed attempts on one
₹2,304 order put ₹2,304 at risk, not ₹6,912 — they are three tries at collecting
one debt, not three debts. Summing the attempt ledger is the natural mistake and
is explicitly tested against.

For a subscription risk, `order_id` and `order_state` are `null`, `order_ref`
carries the subscription reference, and `attempts` / `integrity` / `money` are
omitted.

---

## `GET /api/v1/orders/{order_id}/timeline`

The reconstructed causal timeline plus its delivery forensics.

```jsonc
{
  "order_id": "8f52032a-46d2-483e-833e-a34459a9a0e7",
  "merchant_id": "4c415c25-c5fe-4800-9948-fecdcb970084",
  "customer_id": "c5af734d-29b1-4625-9d91-550660274ccf",
  "order_ref": "sim_order_42_1",
  "state": "attempted",
  "currency": "INR",
  "money": {
    "order_amount_minor": 230400,
    "captured_minor": 0,
    "failed_minor": 230400,
    "refunded_minor": 0,
    "recovered_minor": 0,
    "outstanding_minor": 230400
  },
  "reached_terminal_success": false,
  "has_capture": false,
  "has_order_paid": false,
  "has_refund": false,
  "entries": [
    {
      "causal_position": 1,
      "delivery_position": 1,
      "external_event_id": "sim_evt_S04_42_000001",
      "event_type": "order.created",
      "occurred_at": "2026-01-01T00:00:00Z",
      "received_at": "2026-01-01T00:00:04Z",
      "delay_seconds": 4,
      "summary": "Order created"
    },
    {
      "causal_position": 3,
      "delivery_position": 3,
      "external_event_id": "sim_evt_S04_42_000003",
      "event_type": "payment.failed",
      "occurred_at": "2026-01-01T00:00:33Z",
      "received_at": "2026-01-01T00:00:34Z",
      "delay_seconds": 1,
      "summary": "Payment failed — card_declined"
    }
  ],
  "attempts": [ /* same Attempt[] shape as evidence */ ],
  "integrity": {
    "duplicate_deliveries": 0,
    "out_of_order_deliveries": 0,
    "max_delivery_lag_seconds": 5,
    "inferred_gaps": 0
  },
  "events_examined": 7
}
```

*(Entries elided; the real response carries all seven.)*

**Two orderings travel together and must never be conflated.** `entries` is
always sorted by `causal_position` — what actually happened. `delivery_position`
records where each event sat in arrival order. Under out-of-order delivery
(scenario `S08`) they diverge, and that divergence is the thing worth rendering:
**draw the causal sequence, badge the delivery anomalies.**

`state` is derived from events, never read from `orders.status`. Terminal states
are sticky and ranked: `created` < `attempted` < `abandoned`/`cancelled` <
`paid` < `refunded`.

`404` if the order has no events.

---

## Confidence is not a probability

`confidence_bps` is a **synthetic/demo heuristic**. It does not estimate how
likely a risk is to be real, because nothing in this project has been calibrated
against outcomes that would justify such a claim. It is a transparent,
reproducible measure of *how much evidence supports the finding*.

Every response carrying it also carries
`confidence_is_synthetic_heuristic: true`. The flag exists so a consumer cannot
render the score as a probability by accident. **Do not display it as a
percentage likelihood, and do not label it "confidence that this is real".**
"Evidence strength" is the honest label.

The arithmetic is integer addition and clamping, nothing else:

| Component | Basis points |
|---|---|
| base, repeated payment failure | 6000 |
| base, checkout abandonment | 5500 |
| base, subscription payment failure | 6000 |
| base, reconciliation mismatch | 7000 |
| each failure beyond the two required to fire | +1000 (max +3000) |
| explicit `checkout.abandoned` event rather than inferred silence | +2000 |
| subscription halted by the provider | +1500 |
| each event known to be missing | −750 (max −3000) |

Clamped to 0–10000. The worked example above — `7000` for `S04` — is
`6000 base + 1000` for the third failure.

**Order amount is deliberately absent.** A high-value order is not *stronger
evidence* that something went wrong. Prioritisation is the amount's job, not
confidence's; sort by `amount_at_risk_minor` for that.

Integers rather than floats because Phase 7 policy thresholds will gate money on
this value, and two runs that should agree must agree exactly.

---

## Resolution semantics

A detector that fires and never retracts leaves a permanently wrong open risk
the moment a late capture arrives. Every detection run therefore reconciles
current findings against stored ones:

| Stored risk | Timeline now says | Outcome |
|---|---|---|
| open | rule still fires | **unchanged** — amount and confidence refreshed, `detected_at` preserved |
| open | rule fires, but past the 30-day expiry window | **expired**, `amount_recovered_minor: 0` |
| open | recovery action executed *and* payment captured | **recovered**, with the recovered amount |
| open | payment captured, no recovery action | **recovered** — "collected without intervention" |
| open | `order.paid` arrived after the grace period (reconciliation only) | **recovered**, `amount_recovered_minor: 0` — the event was late, not missing |
| open | rule no longer fires, nothing collected | **false_positive** — the evidence did not hold |
| terminal | anything | untouched. A closed risk is never silently reopened. |

Subscriptions resolve on a subsequent successful billing cycle.

`recovered` is a statement that **money arrived, verified from the timeline**.
It is never an assertion that a recovery action was approved or executed.

---

## Errors

Standard FastAPI shapes. `422` for validation, `404` for a missing resource.

| Case | Status | Body |
|---|---|---|
| Unknown filter value | `422` | `{"detail": "unknown risk_type 'nope'"}` |
| Risk not found | `404` | `{"detail": "risk <uuid> not found"}` |
| Order has no events | `404` | `{"detail": "no events found for order <uuid>"}` |
| Event references unknown merchant | `422` | `{"detail": "events reference unknown merchants: [...]"}` |

Ground truth submitted to ingestion returns `422` with the standard Pydantic
`detail` array:

```json
{
  "type": "value_error",
  "loc": ["body"],
  "msg": "Value error, ground_truth must not be submitted to ingestion: it is evaluation data and must never reach detection input. Strip it and resubmit."
}
```

---

## Security posture

**There is no authentication on this API.** No API key, no bearer token, no
session; `/openapi.json` reports no `securitySchemes` because none exist. Every
endpoint is open to anyone who can reach the port, including
`POST /api/v1/ingest/simulation`, which writes rows.

This is a deliberate, documented Phase 3 gap, not an oversight. Authentication
was not in the approved Phase 3 scope, and adding it unasked would have been an
unrelated architectural change. The consequence must be stated plainly:

- **Bind to localhost only.** Do not expose this to a network or deploy it
  publicly as it stands.
- **Do not point it at anything but `revtrace_dev` / `revtrace_test`.**
- Authentication, per-merchant authorization, and rate limiting are prerequisites
  for any deployment beyond a local demo. `merchant_id` is currently a *filter
  parameter*, not a *tenant boundary* — any caller can read any merchant's risks.

What the API **cannot** do, regardless of caller, is act on money. There is no
endpoint that approves, executes, refunds, retries, contacts a customer, or
calls Razorpay. Detection writes to `revenue_risks` and nothing else;
`recovery_cases`, `recovery_actions`, and `audit_events` stay at zero rows,
verified by test. The absence of authentication is a confidentiality and
integrity-of-data problem, not a path to unauthorised money movement.

## What this API deliberately does not return

No `recommended_action`, `expected_recovery`, `policy_status`, `approved`, or
`executed` — anywhere, on any response. Those belong to Phases 6 and 7, and a
detection response carrying them would assert an authority detection does not
have. **Identifying a risk is not authorising a response to it.**

A test asserts that no detection output carries any of those field names.
