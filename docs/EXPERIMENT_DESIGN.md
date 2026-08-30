# Experiment pre-registration — EXP-001

**SYNTHETIC / DEMO EVALUATION.** Every number this experiment will produce comes
from a generator whose parameters were written by the same person evaluating
them. See [Limitations](#limitations).

This document is the pre-registration. It is written **before** any data is
generated, assigned, or analysed. Once `experiments.locked_at` is set, the
specification below is frozen — enforced by a database trigger, not by
convention. There is no unlock.

---

## Why pre-register at all

Every recovery product in this category reports **gross recovery**: money that
arrived among customers it contacted. That number is not falsifiable, because it
includes everyone who would have paid anyway. In India, where UPI self-retry is
habitual, that share is large.

The honest number requires a control group and a specification fixed in advance.
Without pre-registration, a disappointing result can be rescued after the fact by
changing the metric, the window, or the subgroup — and nobody can tell from the
outside that it was. Locking the specification is what makes the eventual number
mean something.

---

## 1. Hypothesis

> Executing a recovery intervention on a case with a detected revenue risk
> increases the probability that the order is paid within the observation
> window, relative to an untreated holdout drawn from the same population.

Stated so it can fail. A null or negative result is a publishable outcome of
this experiment, not a bug in it — and one planted segment is expected to show a
negative effect.

## 2. Metrics

| Role | Metric | Definition |
|---|---|---|
| **Primary** | `recovery_rate` | Share of cases with `case_outcomes.recovered = true` at seal |
| Secondary | `recovered_amount_mean` | Mean `recovered_amount` in minor units, **including zeros** |
| Secondary | `incremental_recovered` | `(mean_amt_treat − mean_amt_control) × n_treat` |
| Secondary | `credited_not_earned` | `gross_recovered − incremental_recovered` |
| **Harm** | `mandate_cancellation_rate` | Share with `harm_mandate_cancelled = true` |
| **Harm** | `opt_out_rate` | Share with `harm_opted_out = true` |
| **Harm** | `complaint_rate` | Share with `harm_complaint = true` |

Zeros are included in every mean. Dropping non-recoveries would measure "how
much did payers pay", which is a different and much flatterer question.

Harm metrics are pre-registered as first-class outcomes, not as a footnote. A
treatment that recovers money while destroying mandates is not a success, and
deciding that after seeing the data would be exactly the manoeuvre this document
exists to prevent.

## 3. Unit of randomisation

One **case** — a single detected revenue risk on a single order. Not a customer
and not an order, because a customer may accumulate several risks and the
intervention is taken per case.

Concretely, that is a row in **`revenue_risks`**, and `case_assignments`,
`case_outcomes` and `uplift_scores` all reference it by `risk_id`. It is
deliberately *not* `recovery_cases`: a recovery case is created only when the
pipeline decides to act, so anchoring the assignment there would mean a unit
that never reached that point had no assignment and silently left the
denominator. Fixing the population at detection is what makes the
intention-to-treat analysis below sound.

A consequence worth stating: a holdout unit never gets a `recovery_case` row at
all. A holdout is an untreated control, not an abstention, and the two must not
be conflated in the do-no-harm ledger.

*Known limitation:* with several cases per customer, treatment on one could
affect another (interference / SUTVA violation). The Phase 2 catalogue gives
each customer at most one order, so this does not bite in the synthetic run. It
would need a customer-level randomisation in production, and it is recorded here
rather than discovered later.

## 4. Arms and allocation

| Arm | Share | Meaning |
|---|---|---|
| `treatment` | 50% | Eligible for intervention, subject to the policy gate |
| `holdout` | 50% | No intervention. Ever. Enforced by `ck_recovery_cases_holdout_never_acts` |

**50/50 for the benchmark.** Sample is free in simulation and balanced
allocation maximises power for a given N. Production would use 5–10%; the schema
stores `holdout_bps` so that is a configuration change, and the holdout sizing
calculator (Day 4) quantifies the revenue forgone at each level.

## 5. Assignment mechanism

```
stratum_key = f"{risk_type}|{amount_band}"
h           = sha256(f"{risk_id}:{experiment_id}:{ASSIGNMENT_SALT}")
bucket      = int(h[:8], 16) % 10000
arm         = "holdout" if bucket < holdout_bps else "treatment"
```

Hashed rather than random, which buys two properties at once:

- **Reproducible.** An auditor can recompute any assignment from stored inputs.
- **Idempotent.** Re-running detection produces the same `risk_id` — detection
  upserts on the natural key `(merchant_id, order_id, risk_type)` — so a
  duplicate or out-of-order webhook lands in the same arm. Randomisation and
  webhook idempotency are satisfied by the same line.

`ASSIGNMENT_SALT` is a fixed configuration value, never generated at runtime.
Changing it re-randomises every assignment and is therefore a breaking
reassignment, not a tuning knob. The demo default is non-secret and documented.

**Amount bands (minor units):**
`<50000 | 50000–200000 | 200000–500000 | 500000–1500000 | >1500000`

The 15,00,000 paise (₹15,000) boundary is deliberate: it is the RBI
additional-factor-authentication threshold for recurring debits, so cases either
side of it face different regulatory constraints and must not be pooled blindly.

### Why the strata are `risk_type` and `amount_band` only

An earlier draft of this document proposed stratifying on
`risk_type | amount_band | payment_method | issuer | customer_tier`. Auditing it
against the schema before locking showed two of those cannot be computed:

| Field | Status |
|---|---|
| `risk_type` | stored on `revenue_risks` |
| `amount_band` | derived from `revenue_risks.amount_at_risk` |
| `payment_method` | reachable **only** for risks that have payment attempts |
| `issuer` | **no such column anywhere in the schema** |
| `customer_tier` | **no such column anywhere in the schema** |

`payment_method` is not merely missing, it is definitionally absent for two of
the four detectable risk types: a `checkout_abandonment` has no payment attempt
by construction, and a `subscription_payment_failure` carries no `order_id` at
all, so there is no attempt to read a method from. Stratifying on a key that is
null for half the population would produce strata that mean different things in
different arms.

So the strata are the two covariates that are universal, stored, and already
pre-registered as analysis subgroups. **`payment_method` remains an analysis and
balance covariate** — it appears in the SMD table and in subgroup analysis,
where a missing value is reported as such rather than silently pooled.

This correction was made **before `locked_at` was set**, which is the only time
it can legitimately be made. After the lock, a stratum key that could not be
computed would have meant either an unrunnable experiment or a post-hoc
amendment — and a post-hoc amendment is precisely what pre-registration exists
to prevent.

### Excluded from assignment

`reconciliation_mismatch` is never assigned: its `amount_at_risk` is zero by
definition (ADR 0007 — the money arrived, only the bookkeeping is broken), so
there is nothing to recover and it would dilute the effect estimate toward zero.
`payment_degradation` has no detector and cannot arise.

## 6. Observation windows

| Risk type | Window |
|---|---|
| `repeated_payment_failure` | 72 h |
| `checkout_abandonment` | 24 h |
| `subscription_payment_failure` | 168 h |
| `reconciliation_mismatch` | Excluded — `amount_at_risk = 0`, nothing to recover |

Windows open at detection. A sweeper closes them and sets `sealed = true`.
**No unsealed outcome may enter an analysis.** Events arriving after seal are
recorded in the audit trail as `decision_type = 'verify'` with
`action = 'LATE_EVENT'`, and excluded from the estimate; the evaluation report
states the count rather than quietly absorbing them.

### Audit linkage

Assignment and sealing are both events about a **risk**, not about a recovery
case, so `audit_events` carries a nullable `risk_id → revenue_risks`. The
existing `case_id → recovery_cases` is retained and unchanged, for the
recovery-case events it was built for.

Without this an assignment entry would have to store `case_id = NULL` and lose
all linkage — and under the risk-anchored design a holdout unit never gets a
recovery case at all, so `case_id` could never identify it.

## 7. Power and sample size

Pre-registered parameters:

| Parameter | Value | Stored as |
|---|---|---|
| α | 0.05 | `alpha_bps = 500` |
| Power (1−β) | 0.80 | `power_bps = 8000` |
| MDE | 10 pp | `mde_bps = 1000` |
| Baseline (control) | 0.35 assumed | — |
| Planned N per arm | **384** | `planned_n_per_arm = 384` |

```
n_per_arm = (z_{1−α/2} + z_{1−β})² · [p_c(1−p_c) + p_t(1−p_t)] / (p_t − p_c)²
          = 7.84 · [0.35·0.65 + 0.45·0.55] / 0.10²
          ≈ 384
```

The run targets 8,000–12,000 cases, far above the minimum, so the binding
constraint will be subgroup power rather than overall power. Subgroups below the
minimum are reported as underpowered rather than as estimates.

All parameters are integer basis points. The project bans float columns
(ADR 0001), and a pre-registered threshold is precisely the value that must
compare exactly across runs.

## 8. Analysis plan

**Primary: intention-to-treat.** Every case is analysed in its assigned arm,
including cases where execution failed. Reclassifying a failed execution as a
control would inflate the measured effect, and `case_outcomes.execution_failed`
exists so the non-compliance rate can be reported rather than hidden.

**Secondary: per-protocol**, reported alongside ITT with the difference stated
explicitly.

- **Effect estimate:** difference in proportions.
- **Interval:** bootstrap, 10,000 resamples within arm, percentile method.
  Bootstrap rather than Wald because revenue is heavy-tailed and the same
  machinery must serve both the rate and the amount.
- **Test:** two-proportion test on the primary metric.
- **Multiplicity:** subgroup analyses controlled with Benjamini–Hochberg at
  q = 0.10. Pre-registered subgroups: amount band, payment method, risk type.
  Anything else is exploratory and labelled as such.
- **Balance:** standardised mean difference per covariate across arms, computed
  as an integer `smd_bps` and flagged when `|smd_bps| > 1000` — the basis-point
  form of the conventional `|SMD| > 0.1`. Integer arithmetic with an integer
  square root, so a balance verdict is exactly reproducible and no float enters
  a comparison. Balance covariates: `risk_type`, `amount_band`,
  `amount_at_risk`, `confidence_bps`, and `payment_method` where present.

**No number is reported without an interval.** Anywhere, including the demo.

## 9. Stopping rule

**Fixed horizon.** The experiment closes when every case created before the
enrolment cut-off has a sealed window. No interim look changes that.

If `n < planned_n_per_arm`, `experiment_results.is_underpowered` is set and the
UI must display `INTERIM — UNDERPOWERED` instead of a point estimate. There is
no early stop for success: peeking and stopping on a favourable reading inflates
the false-positive rate, and the whole claim here rests on not doing that.

## 10. Ground-truth isolation

The simulator writes both potential outcomes to `case_outcomes.truth_*`. These
are the answer key. They are readable **only** by `reporting/evaluation.py` and
are never an input to estimation — no module under `app/causal/` or
`app/engine/` may reference them, enforced by a test that scans the application
package. If they reached the estimator, every number here would be circular.

## 11. Lifecycle

```
draft ──lock──▶ locked ──start──▶ running ──close──▶ closed
                   │                                    ▲
                   └──────────── close ─────────────────┘
```

`locked_at` is the pre-registration timestamp cited by the evaluation report.
After it, the specification is immutable — enforced by the
`trg_experiments_lock_guard` trigger, a CHECK requiring the timestamp on any
non-draft row, and refusal in `app/experiments/registry.py`. Status, `started_at`
and `closed_at` remain mutable, because the lifecycle must still advance.

## Limitations

Stated here, not buried, because they are what make the rest credible.

1. **All results are synthetic.** The simulator's segment parameters are
   assumptions, not measurements. The ground-truth comparison is only possible
   because the ground truth was written by hand.
2. **The sleeping-dog effect is planted.** Recovering it validates the
   estimator, not the world.
3. **Real customer response to a nudge is unobservable in test mode.**
4. **Detection precision is measured against the same generator** that produced
   the data.
5. **A production holdout costs real money.** The calculator quantifies that
   trade-off; it does not remove it.
6. **Uplift models drift.** Quadrant labels need continuous re-estimation, and a
   label learned here has no shelf life in production.

---

## Lock record

| Field | Value |
|---|---|
| Experiment | EXP-001 |
| Status | **DRAFT — not yet locked** |
| `locked_at` | *(set when the registry locks it; nothing may change after)* |

Day 1 ships the specification and the machinery that enforces it. The experiment
row is created and locked as part of the Day 3 assignment wiring, once the
strata definition can be populated from real detection output.
