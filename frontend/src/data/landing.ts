/**
 * Every figure the landing page displays, derived from committed artifacts.
 *
 * **The hard rule this module exists to enforce: no numeric statistic may be
 * typed into TSX.** A number written by hand in a component is a number nobody
 * can trace, and it will drift from the artifact the moment either changes.
 * Components import from here; this file reads the artifacts and nothing else.
 *
 * Two consequences worth stating, because they are the point rather than
 * limitations of it:
 *
 * **Nothing here computes a statistic.** The backend did that in integer
 * arithmetic and its results are the artifact. This module selects, renames and
 * re-shapes; it never derives a new claim. The one arithmetic operation it
 * performs is subtracting two committed counts to show a difference, and that
 * subtraction is named where it happens.
 *
 * **An absent value stays absent.** `null` means a quantity is undefined — an
 * undefined Qini coefficient is not a coefficient of zero — and this module
 * propagates `null` rather than substituting a default. Anything the artifacts
 * do not contain is typed as `null` and documented as missing, never invented.
 *
 * Money is an integer count of minor units; rates and effects are integer basis
 * points. `lib/format.ts` renders both. Nothing here divides.
 */

import { demoRun, evaluation, gateComparison } from "./artifacts";

/* ------------------------------------------------------------------ *
 * Provenance
 * ------------------------------------------------------------------ */

/**
 * What every number on the page is, and is not.
 *
 * The labels are the backend's own words, carried on the artifacts. A
 * disclosure the frontend composed would be a claim nobody checked.
 */
export const provenance = {
  /** "SYNTHETIC / DEMO EVALUATION" */
  evaluationLabel: evaluation.label,
  /** "SYNTHETIC / DEMO EVALUATION — gate and spend decisions" */
  gateLabel: gateComparison.label,
  /** "DEMO / SYNTHETIC / OFFLINE — not a Razorpay transaction" */
  demoProvenance: demoRun.provenance,
  experimentId: evaluation.experiment.id,
  experimentName: evaluation.experiment.name,
  /** The pre-registered hypothesis, frozen before the run. */
  hypothesis: evaluation.experiment.hypothesis,
  lockedAt: evaluation.experiment.locked_at,
  seed: gateComparison.seed,
  bootstrapSeed: evaluation.bootstrap.seed,
  resamples: evaluation.bootstrap.resamples,
  migration: gateComparison.provenance.migration,
} as const;

/* ------------------------------------------------------------------ *
 * Section: the incrementality ledger
 * ------------------------------------------------------------------ */

/**
 * The three quantities the whole pitch rests on.
 *
 * `creditedNotEarned` is the gap between gross and incremental — the money a
 * conventional recovery dashboard would have taken credit for. It is read from
 * the artifact rather than subtracted here, because the backend computed it and
 * a second computation could disagree.
 */
export const ledger = {
  grossRecovered: evaluation.ledger.gross_recovered,
  incrementalRecovered: evaluation.ledger.incremental_recovered,
  incrementalCiLow: evaluation.ledger.incremental_ci_low,
  incrementalCiHigh: evaluation.ledger.incremental_ci_high,
  creditedNotEarned: evaluation.ledger.credited_not_earned,
  /** Share of gross that was credited but not earned, in basis points. */
  creditedShareBps: evaluation.ledger.credited_share_bps,
  meanTreatment: evaluation.ledger.mean_treatment,
  meanHoldout: evaluation.ledger.mean_holdout,
  treatmentCount: evaluation.ledger.n_treatment,
  holdoutCount: evaluation.ledger.n_holdout,
} as const;

/* ------------------------------------------------------------------ *
 * Section: proven vs unproven
 * ------------------------------------------------------------------ */

/**
 * The primary causal result: the effect on recovery rate, with its interval.
 *
 * `pValueMicros` is a p-value carried as millionths; zero means "smaller than
 * this representation can express", not "exactly zero", and the formatter says
 * so.
 */
export const effect = {
  ateBps: evaluation.recovery.ate_bps,
  ciLowBps: evaluation.recovery.ate_ci_low_bps,
  ciHighBps: evaluation.recovery.ate_ci_high_bps,
  rateTreatmentBps: evaluation.recovery.rate_treatment_bps,
  rateHoldoutBps: evaluation.recovery.rate_holdout_bps,
  treatmentCount: evaluation.recovery.n_treatment,
  holdoutCount: evaluation.recovery.n_holdout,
  pValueMicros: evaluation.recovery.p_value_micros,
} as const;

/**
 * The randomised holdout that makes the effect a measurement.
 *
 * `isBalanced` and `isUnderpowered` are the backend's own verdicts, not
 * thresholds re-applied here.
 */
export const experimentDesign = {
  holdoutBps: evaluation.experiment.holdout_bps,
  alphaBps: evaluation.experiment.alpha_bps,
  mdeBps: evaluation.experiment.mde_bps,
  powerBps: evaluation.experiment.power_bps,
  primaryMetric: evaluation.experiment.primary_metric,
  plannedNPerArm: evaluation.power.planned_n_per_arm,
  achievedNPerArm: evaluation.power.achieved_n_per_arm,
  requiredNPerArm: evaluation.power.required_n_per_arm,
  isUnderpowered: evaluation.power.is_underpowered,
  isBalanced: evaluation.balance.is_balanced,
  flaggedCovariates: evaluation.balance.flagged.length,
} as const;

/**
 * The estimate against the planted truth.
 *
 * Only knowable because the truth was written by hand — which is exactly why
 * this belongs beside the limitations rather than on its own.
 */
export const accuracy = {
  estimatedAteBps: evaluation.accuracy.estimated_ate_bps,
  trueAteBps: evaluation.accuracy.true_ate_bps,
  errorBps: evaluation.accuracy.error_bps,
  intervalCoversTheTruth: evaluation.accuracy.interval_covers_the_truth,
} as const;

/**
 * Sections the backend **declined** to report, and why.
 *
 * On the page for the same reason they are in the artifact: a report that
 * silently omits what it could not compute reads as a report that computed
 * everything.
 */
export const deferred = evaluation.deferred;

/* ------------------------------------------------------------------ *
 * Section: abstention — the gate
 * ------------------------------------------------------------------ */

/**
 * What the gate did, against acting on everything.
 *
 * `actionsAvoided` and `costAvoidedMinor` are read from the artifact, not
 * recomputed. `spendReductionBps` likewise.
 */
export const gate = {
  caseCount: gateComparison.case_count,
  treatmentUnits: gateComparison.treatment_units,
  intervention: gateComparison.intervention,
  unitCostMinor: gateComparison.unit_cost,

  gateOffActed: gateComparison.gate_off.acted,
  gateOffAbstained: gateComparison.gate_off.abstained,
  gateOffCostMinor: gateComparison.gate_off_cost_minor,

  gateOnActed: gateComparison.gate_on.acted,
  gateOnAbstained: gateComparison.gate_on.abstained,
  gateOnOrdinaryActed: gateComparison.gate_on.ordinary_acted,
  gateOnExplored: gateComparison.gate_on.explored,
  gateOnCostMinor: gateComparison.gate_on_cost_minor,

  actionsAvoided: gateComparison.action_reduction,
  actionReductionBps: gateComparison.action_reduction_bps,
  costAvoidedMinor: gateComparison.cost_avoided_minor,
  explorationBudgetBps: gateComparison.exploration_budget_bps,
  grayZonePolicy: gateComparison.provenance.gray_zone_policy,
  policyRevision: gateComparison.provenance.policy_revision,
} as const;

/**
 * Why the gate declined, by named reason.
 *
 * Ordered largest first so the page never has to sort — and so a reason is
 * never dropped for fitting badly.
 */
export const abstentionReasons = (
  Object.entries(gateComparison.abstention_reasons) as [string, number][]
)
  .map(([reason, count]) => ({ reason, count }))
  .sort((a, b) => b.count - a.count);

/** The gray zone: the hardest decision the gate makes, and how it split. */
export const grayZone = {
  total: gateComparison.gray_zone.total,
  abstained: gateComparison.gray_zone.abstained,
  explored: gateComparison.gray_zone.explored,
  ordinaryActed: gateComparison.gray_zone.ordinary_acted,
  selfRecoveryLikely: gateComparison.gray_zone.self_recovery_likely,
  upliftNotSignificant: gateComparison.gray_zone.uplift_not_significant,
} as const;

/** Every quadrant's disposition, for a distribution the page can render. */
export const quadrantDisposition = Object.entries(gateComparison.by_quadrant)
  .map(([quadrant, d]) => ({ quadrant, ...d }))
  .sort((a, b) => b.total - a.total);

/* ------------------------------------------------------------------ *
 * Section: the uplift model
 * ------------------------------------------------------------------ */

/**
 * Uplift figures, or `null` when the run fitted no model.
 *
 * `AvailableReport.uplift` is nullable in the contract, so this is narrowed
 * once here rather than at every call site. **Every consumer must handle
 * `null`** — that is the contract, not an inconvenience.
 */
const upliftArtifact = evaluation.uplift;

export const uplift =
  upliftArtifact === null
    ? null
    : ({
        modelVersion: upliftArtifact.model.version,
        folds: upliftArtifact.model.folds,
        nScored: upliftArtifact.model.n_scored,
        distinctCells: upliftArtifact.ladder.distinct_cells,
        globalFallbacks: upliftArtifact.ladder.global_fallbacks,
        quadrantCounts: upliftArtifact.quadrant_counts,
        /**
         * `null` when Q(N) is exactly zero — undefined, not zero. There was no
         * incremental recovery to apportion, which is a different statement
         * from a ranking that did no better than chance.
         */
        qiniCoefficientBps: upliftArtifact.qini.qini_coefficient_bps,
        qiniIsDefined: upliftArtifact.qini.is_defined,
        qiniBeatsRandom: upliftArtifact.qini.beats_random,
        topCaptureShareBps: upliftArtifact.top_capture.share_bps,
        topCaptureBps: upliftArtifact.top_capture.capture_bps,
        accepted: upliftArtifact.acceptance.accepted,
        acceptanceCriteria: upliftArtifact.acceptance.criteria,
        limitations: upliftArtifact.limitations,
      } as const);

/* ------------------------------------------------------------------ *
 * Section: the live demo
 * ------------------------------------------------------------------ */

/**
 * The six captured demo steps, for rendering **without the backend**.
 *
 * The landing page must tell the whole recovery story as static content; the
 * live run is an optional, explicit interaction on top. This is a real captured
 * run from `run_capture_demo.py`, not prose describing one.
 */
export const demo = {
  steps: demoRun.steps,
  finalStatus: demoRun.final_status,
  committed: demoRun.committed,
  database: demoRun.database,
  provenance: demoRun.provenance,
  capturedAt: demoRun.captured_at,
  note: demoRun.note,
} as const;

/* ------------------------------------------------------------------ *
 * Section: limitations
 * ------------------------------------------------------------------ */

/**
 * Everything these numbers do not establish.
 *
 * Two lists, kept separate because they qualify different claims: the run's own
 * limitations, and the uplift model's. Both come from the artifact. The page
 * must not paraphrase them — a limitation reworded for tone is a limitation
 * weakened.
 */
export const limitations = {
  run: evaluation.limitations,
  uplift: uplift?.limitations ?? [],
} as const;

/* ------------------------------------------------------------------ *
 * Section: the AI boundary
 * ------------------------------------------------------------------ */

/**
 * The Day 3 live hypothesis capture.
 *
 * **The numeric detail was never persisted.** `run_hypothesis.py` rolls back by
 * default and no commit was ever authorised, so no `audit_events` row exists in
 * any database — verified, not assumed. What survives is the classification
 * below, which is why every numeric field here is `null` rather than a number
 * someone reconstructed from a transcript.
 *
 * The claim the capture made was that this cell's uplift interval lies entirely
 * above the population effect. Deterministic code checked the integers it cited
 * against the computed cell statistics and returned `confirmed`. **The count of
 * checked integers is deliberately not stated here**: it is not in any
 * artifact, and a number recalled rather than read is exactly what this project
 * refuses everywhere else.
 *
 * To make this section quantitative, re-run `run_hypothesis.py` against the
 * canonical population with persistence enabled and export the resulting
 * `audit_events` row as an artifact.
 */
export const hypothesisCapture = {
  cellKey: "card_declined|card",
  claim: "higher_uplift_than_population",
  verdict: "confirmed",
  rule: "interval_lies_entirely_above_the_population_effect",
  /** The model, from the agent's pinned constant. No credential is implied. */
  provider: "OpenRouter",
  model: "nvidia/nemotron-3-super-120b-a12b:free",
  /** Not persisted. Null, never a placeholder. */
  citedIntegerCount: null,
  cellStatistics: null,
  capturedAt: null,
} as const;

/* ------------------------------------------------------------------ *
 * What the artifacts do not contain
 * ------------------------------------------------------------------ */

/**
 * Content the landing page needs that **no artifact provides**.
 *
 * Named here rather than typed into a component, so the gap is visible in the
 * data layer instead of hiding as prose in TSX. Each of these is narrative, not
 * numeric — there is no figure to get wrong — but the source still has to be a
 * decision someone makes, not a sentence a component invented.
 *
 * - **The security bug.** The cross-tenant webhook defect is described in
 *   `README.md` §5 and in `services/verification/service.py`'s docstring.
 *   Neither is machine-readable. It carries no statistic.
 * - **Breakage lessons.** `docs/BREAKAGE.md` is prose, not structured data.
 * - **The demo's six-step narrative.** Available and structured — see `demo`.
 */
export const narrativeSources = {
  securityBug: "README.md §5 · app/services/verification/service.py",
  breakage: "docs/BREAKAGE.md",
} as const;
