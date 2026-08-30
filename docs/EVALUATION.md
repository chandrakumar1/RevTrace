# SYNTHETIC / DEMO EVALUATION

Every number below comes from a generated population with planted effects. None of it is evidence about real customers.

## 1. Experiment

- **Name:** BENCH-seed42-n10000
- **Id:** `2b3e9c9f-60e8-5413-adc2-456b89e017b1`
- **Hypothesis:** Creating a payment link on a repeated payment failure increases the probability of payment within 72 hours, relative to an untreated holdout.
- **Primary metric:** `recovery_rate`
- **Locked at:** 2026-01-01T03:30:00+05:30
- **Started at:** 2026-01-01T04:30:00+05:30
- **Holdout:** 50.00% of enrolled units

## 2. Power

| Quantity | Value |
|---|---|
| Achieved N per arm | 4,956 |
| Planned N per arm | 384 |
| Required N per arm (exact formula) | 380 |
| alpha | 5.00% |
| Power | 80.00% |
| Pre-registered MDE | 10.00% |
| Detectable effect at achieved N | 2.74% |
| Achieved CI width | 3.87% |
| Underpowered | no |

## 3. Covariate balance

Flagged when `|SMD| > 0.10`. Verdict: **BALANCED**.

| Covariate | Kind | Worst SMD | Flagged |
|---|---|---|---|
| `risk_type` | categorical | 0.00% | no |
| `amount_band` | categorical | -1.30% | no |
| `amount_at_risk` | continuous | 1.35% | no |
| `confidence_bps` | continuous | 0.00% | no |
| `payment_method` | categorical | 2.40% | no |

## 4. The three headline numbers

| Figure | Amount |
|---|---|
| Gross recovered (treated arm) | Rs 13,354,580.93 |
| **Incremental recovered** | Rs 4,478,806.05 CI [Rs 3,185,424.26, Rs 5,804,028.54] |
| **Credited-not-earned** | Rs 8,875,774.88 |
| Share of gross never caused | 66.46% |

Gross is what a recovery dashboard reports. Credited-not-earned is the part of it that would have arrived anyway.

## 5. Primary metric — recovery rate (ITT)

| Quantity | Value |
|---|---|
| Treated | 52.87% (2,667 of 5,044) |
| Holdout | 37.23% (1,845 of 4,956) |
| **ATE** | **15.64%** |
| 95% CI | [13.70%, 17.57%] |
| p-value | < 0.000001 |

## 6. Harm — mandate cancellation

Pre-registered as a first-class outcome, not a footnote.

| Quantity | Value |
|---|---|
| Treated | 2.64% |
| Holdout | 0.73% |
| **Harm ATE** | **1.91%** |
| 95% CI | [1.41%, 2.42%] |

## 7. ITT and per-protocol side by side

| Analysis | N treated | N holdout | ATE | 95% CI |
|---|---|---|---|---|
| ITT (primary) | 5,044 | 4,956 | 15.64% | [13.70%, 17.57%] |
| Per-protocol | 5,044 | 4,956 | 15.64% | [13.70%, 17.57%] |

Non-compliance: 0.00% (0 units excluded from per-protocol).

## 8. Estimate against the known effect

**Only possible because the data is synthetic.** The true effect was written by hand; no production system can produce this section.

| Quantity | Value |
|---|---|
| Estimated ATE | 15.64% |
| True ATE | 14.09% |
| Error | 1.55% |
| Interval contains the true value | yes |
| True harm ATE | 1.88% |
| Self-recovery share (true) | 38.28% |

### Known effect by planted stratum

| Stratum | N | True ATE | True harm ATE |
|---|---|---|---|
| `expired_or_blocked_card` | 1,536 | 54.62% | 0.78% |
| `high_value_customer` | 533 | 24.01% | -0.19% |
| `insufficient_funds_salary_cycle` | 2,001 | 17.34% | 0.85% |
| `intentional_churner` | 959 | 1.98% | 0.84% |
| `issuer_downtime` | 1,043 | 0.87% | 0.19% |
| `low_engagement_mandate_holder` | 1,409 | -0.64% | 9.79% |
| `transient_upi_timeout` | 2,519 | 3.02% | 0.47% |

## 8a. Uplift model

| Quantity | Value |
|---|---|
| Model version | `cellrate-1/k5/fc+pm` |
| Cross-fitting folds | 5 |
| Units scored | 10,000 |
| alpha | 5.00% |
| MDE for cell qualification | 10.00% |
| Bootstrap seed / resamples | `20260830` / 10,000 |

## 8b. Qini — does the ranking beat chance?

| Quantity | Value |
|---|---|
| Qini coefficient | 14.89% |
| Q(N) — total incremental recoveries | 789 |
| Beats random | yes |
| N treated / holdout | 5,044 / 4,956 |

Positive means the ranking captured more incremental recovery than a random order; negative means it did worse. Undefined means `Q(N) == 0` and there was nothing to apportion.

### Top 20.00% capture

| Weighting | k | Captured | Total | Share |
|---|---|---|---|---|
| By unit count | 2,000 | 305 | 789 | 38.66% |
| By recovered amount | 2,000 | Rs 1,206,559.46 | Rs 4,478,806.05 | 26.94% |

The two can disagree. A ranking that promotes many cheap recoveries scores well by count and poorly by money, and only the second is revenue.

## 8c. Quadrant counts

| Quadrant | N | Share |
|---|---|---|
| `persuadable` | 4,702 | 47.02% |
| `sure_thing` | 0 | 0.00% |
| `lost_cause` | 0 | 0.00% |
| `sleeping_dog` | 1,618 | 16.18% |
| `gray_zone` | 3,680 | 36.80% |

**Empty quadrants:** `sure_thing`, `lost_cause`. Listed rather than dropped — a quadrant nothing landed in was still looked for, and a reader must be able to tell that apart from one that was never evaluated.

### By rule

| Rule | N |
|---|---|
| `harm_above_threshold` | 1,618 |
| `significant_uplift_below_ceiling` | 4,702 |
| `undecided` | 3,680 |

## 8d. Gray Zone, by reason

Two different situations produce the same label: a cell that never qualified, and a qualified cell whose result matched no rule. They are counted apart because they mean different things.

Total Gray Zone: **3,680**

| Quadrant rule | N |
|---|---|
| `undecided` | 3,680 |

| Cell qualification reason | N |
|---|---|
| `qualified` | 3,680 |

## 8e. Ladder level and cell usage

| Level | N |
|---|---|
| `failure_code` | 3,904 |
| `failure_code|payment_method` | 6,096 |

Distinct cells scored: **9**. Global fallbacks: **0**.

| Cell | N |
|---|---|
| `insufficient_funds|card` | 1,717 |
| `card_declined|card` | 1,688 |
| `gateway_timeout|upi` | 1,630 |
| `bank_unavailable` | 1,323 |
| `mandate_inactive|upi` | 1,061 |
| `gateway_timeout` | 727 |
| `card_declined` | 649 |
| `insufficient_funds` | 648 |
| `mandate_inactive` | 557 |

## 8f. Harm uplift

Computed in memory to decide sleeping-dog labels, then discarded — there is no column for it, by design. This report is the only place it is visible.

| Quantity | Value |
|---|---|
| Units | 10,000 |
| Minimum | -0.18% |
| Mean | 2.01% |
| Maximum | 8.52% |
| Positive harm uplift | 9,734 |
| Above their fold's threshold | 1,618 |

## 8g. Fold-local thresholds

Each fold's boundaries come from the four folds it trained on, and apply only to the fold it held out. Derived from the whole population instead, a unit's own outcome could move the boundary it is then judged against.

| Fold | Self-recovery ceiling | Low tertile | High tertile | Harm threshold |
|---|---|---|---|---|
| 0 | 37.01% | 15.68% | 63.13% | 2.34% |
| 1 | 37.75% | 16.22% | 63.05% | 2.64% |
| 2 | 37.79% | 16.62% | 63.57% | 2.36% |
| 3 | 37.13% | 16.15% | 63.43% | 2.50% |
| 4 | 36.46% | 15.34% | 62.15% | 2.57% |

## 8h. Quadrant confusion matrix against planted strata

**Only possible because the data is synthetic.** `truth_segment` is read here and nowhere else in the application. Every label being counted was produced by a model that saw only observed features; this comparison happens strictly afterwards and never feeds back.

| Planted stratum | N | `persuadable` | `sure_thing` | `lost_cause` | `sleeping_dog` | `gray_zone` | Modal | Mean uplift |
|---|---|---|---|---|---|---|---|---|
| `expired_or_blocked_card` | 1,536 | 1,267 | 0 | 0 | 89 | 180 | `persuadable` | 27.63% |
| `high_value_customer` | 533 | 450 | 0 | 0 | 32 | 51 | `persuadable` | 19.44% |
| `insufficient_funds_salary_cycle` | 2,001 | 1,635 | 0 | 0 | 115 | 251 | `persuadable` | 19.12% |
| `intentional_churner` | 959 | 778 | 0 | 0 | 62 | 119 | `persuadable` | 27.41% |
| `issuer_downtime` | 1,043 | 116 | 0 | 0 | 71 | 856 | `gray_zone` | 9.04% |
| `low_engagement_mandate_holder` | 1,409 | 145 | 0 | 0 | 1,104 | 160 | `sleeping_dog` | 4.37% |
| `transient_upi_timeout` | 2,519 | 311 | 0 | 0 | 145 | 2,063 | `gray_zone` | 9.09% |

### Acceptance criterion

| Clause | Expected | Observed | Result |
|---|---|---|---|
| low_engagement_mandate_holder is modally a sleeping dog | sleeping_dog | sleeping_dog | **PASS** |
| transient_upi_timeout is not modally persuadable | anything but persuadable | gray_zone | **PASS** |
| mean uplift ranks transient_upi_timeout below insufficient_funds_salary_cycle | strictly lower | 909 bps vs 1912 bps | **PASS** |

Overall: **ACCEPTED**.

The self-recovering stratum is required not to be Persuadable, rather than to carry one specific label. The decision that matters is *do not spend money here*, which Gray Zone and Sure Thing both produce.

### Limitations of the uplift model

These qualify every number in sections 8a-8h.

- Cell-level intervals are nominal 95% and uncorrected for multiplicity. Nine cells are scored per fold and each interval is read on its own, so the chance that at least one is wrong is far above 5%. The Benjamini-Hochberg procedure exists in the estimator layer and is deliberately not applied to per-cell quadrant decisions; treat a single cell's interval as indicative, not as a test that survived correction.
- Sure Thing and Lost Cause are defined as a confidence interval containing zero. At N=10,000 no cell's interval contains zero, so both quadrants come out empty — the definition asks for a null result, and a study this size resolves effects a smaller one would have missed. Distinguishing 'no effect' from 'an effect too small to be worth acting on' needs equivalence testing against a margin, which is future work. The classifier is unchanged; this is a limit of the definition, not a defect in the run.
- Quadrant labels are only as sharp as the features. `intentional_churner` and `expired_or_blocked_card` share the failure code `card_declined`, and no persisted feature separates them, so the merged cell inherits the blocked-card lift and the churner is labelled Persuadable against its planted Lost Cause. This is a feature-resolution limit. It would be resolved by a feature that distinguishes the two, never by retuning the model against the answer key.
- Top-share capture by unit count is not a revenue claim. The top 20% of the ranking holds 38.66% of incremental recoveries but only 26.94% of incremental rupees, so the ranking is better at finding recoveries than at finding valuable ones. Quote the amount-weighted figure whenever the claim is about money.

## 9. Reproduction

- Bootstrap seed: `20260830`
- Bootstrap resamples: 10,000
- Percentile method, resampled within arm, alpha 5.00%

## 10. Deferred sections

Listed rather than omitted. A gap a reader can see is a gap.

- **Detection precision and recall** — the detection benchmark harness lives in the test tree, and the application may not import from it; promoting it is scheduled separately.
- **Net incremental value P&L** — requires a gross margin and an average customer lifetime value. Neither exists anywhere in this codebase, and inventing either would put a made-up number into a money figure.
- **Harm cost** — same missing inputs as the P&L. The harm *effect* is reported below with an interval; only its conversion into money is deferred.
- **Failure scenarios and observed behaviour** — requires the provider adapter and webhook verification, which are a later phase.

## 11. Limitations

- All results are synthetic. The planted parameters are assumptions someone wrote down, not measurements.
- The effects are planted, so recovering them validates the estimator, not the world.
- Real customer response to a nudge is unobservable in a test environment.
- The comparison against the true effect is only possible because the true effect was written by hand. No production system can produce this section.
- A production holdout costs real money. The sizing calculator quantifies that trade-off; it does not remove it.
- Interval calibration was checked on 20 independently seeded populations of 2,000 and the true effect fell inside the 95% interval every time. That is no evidence of under-coverage; it is not a demonstration of exact calibration. Twenty trials cannot separate 95% from 99% — under a true 95% rate, a clean sweep is the single most likely outcome (p = 0.36), and the one-sided lower bound on true coverage is only 86%. Reporting it as '100% calibrated' would overstate what was measured.
- The generator's observable signal was deliberately strengthened after two planted strata proved indistinguishable from the features being persisted: `failure_code` is now written with 70% characteristic / 30% off-characteristic noise. The model was never changed to fit the data, but the data was made learnable, and a real payment stream carries whatever signal it carries.
