/**
 * Types for the backend evaluation report.
 *
 * Every field here was read off the four verified fixtures in `src/fixtures/`.
 * Nothing is invented, and nothing is optional that the backend always sends —
 * a speculative field would be a claim about an API that does not exist yet.
 *
 * Two conventions run through the whole payload:
 *
 * - **Basis points.** Rates, effects and shares are integers out of 10,000.
 *   1564 is 15.64%. The backend does all of its arithmetic in integers so that
 *   no float ever touches a money or probability path; the frontend must not
 *   undo that by dividing.
 * - **Minor units.** Money is an integer count of paise. 447880605 is
 *   ₹44,78,806.05. Format it for display, never compute with it.
 *
 * `null` is meaningful and is never interchangeable with `0`. A null
 * `qini_coefficient_bps` means there was no incremental recovery to apportion,
 * which is a different statement from "the ranking did no better than chance".
 */

/** The five uplift quadrants, as the backend spells them. */
export type Quadrant =
  | "persuadable"
  | "sure_thing"
  | "lost_cause"
  | "sleeping_dog"
  | "gray_zone";

export type QuadrantCounts = Record<Quadrant, number>;

export interface Experiment {
  id: string;
  name: string;
  hypothesis: string;
  primary_metric: string;
  locked_at: string | null;
  started_at: string | null;
  alpha_bps: number;
  power_bps: number;
  mde_bps: number;
  planned_n_per_arm: number;
  holdout_bps: number;
}

export interface Power {
  achieved_n_per_arm: number;
  planned_n_per_arm: number;
  required_n_per_arm: number;
  detectable_mde_bps: number;
  is_underpowered: boolean;
  ci_width_bps: number;
}

/** A rate difference between arms, with its bootstrap interval. */
export interface RateEffect {
  ate_bps: number;
  ate_ci_low_bps: number;
  ate_ci_high_bps: number;
  rate_treatment_bps: number;
  rate_holdout_bps: number;
  n_treatment: number;
  n_holdout: number;
  p_value_micros: number;
  bootstrap_seed: number;
  bootstrap_resamples: number;
}

/** The incrementality ledger. All amounts are minor units. */
export interface Ledger {
  gross_recovered: number;
  incremental_recovered: number;
  incremental_ci_low: number;
  incremental_ci_high: number;
  credited_not_earned: number;
  credited_share_bps: number;
  mean_treatment: number;
  mean_holdout: number;
  n_treatment: number;
  n_holdout: number;
  bootstrap_seed: number;
  bootstrap_resamples: number;
}

export interface PerProtocol {
  recovery: RateEffect;
  non_compliance_bps: number;
  excluded_total: number;
}

export interface BalanceLevel {
  level: string;
  treatment_count: number;
  holdout_count: number;
  smd_bps: number | null;
  undefined_reason: string | null;
}

export interface BalanceCovariate {
  name: string;
  kind: string;
  smd_bps: number | null;
  undefined_reason: string | null;
  levels: BalanceLevel[];
}

export interface Balance {
  experiment_id: string;
  is_balanced: boolean;
  threshold_bps: number;
  treatment_n: number;
  holdout_n: number;
  covariates: BalanceCovariate[];
  flagged: string[];
}

export interface GroundTruth {
  n: number;
  y0_rate_bps: number;
  y1_rate_bps: number;
  true_ate_bps: number;
  harm0_rate_bps: number;
  harm1_rate_bps: number;
  true_harm_ate_bps: number;
  self_recovery_share_bps: number;
}

export interface StratumTruth {
  label: string;
  n: number;
  true_ate_bps: number;
  true_harm_ate_bps: number;
}

export interface Accuracy {
  estimated_ate_bps: number;
  true_ate_bps: number;
  error_bps: number;
  interval_covers_the_truth: boolean;
}

export interface DeferredSection {
  section: string;
  reason: string;
}

// -- the uplift model ------------------------------------------------------

export interface UpliftModel {
  version: string;
  folds: number;
  alpha_bps: number;
  mde_bps: number;
  resamples: number;
  seed: number;
  n_scored: number;
}

/**
 * How much incremental recovery a top prefix of the ranking accounted for.
 *
 * `capture_bps` is `null` when `total` is zero: with nothing to apportion the
 * share has no value, and reporting `0` would read as a measurement.
 */
export interface Capture {
  share_bps: number;
  k: number;
  n: number;
  qini_at_k: number;
  total: number;
  capture_bps: number | null;
}

export interface Qini {
  n: number;
  n_treated: number;
  n_holdout: number;
  qini_total: number;
  /** `null` when `qini_total` is 0 — undefined, not zero. */
  qini_coefficient_bps: number | null;
  is_defined: boolean;
  beats_random: boolean;
  top_capture: Capture;
}

export interface GrayZone {
  total: number;
  by_rule: Record<string, number>;
  by_reason: Record<string, number>;
}

export interface LadderCell {
  cell: string;
  n: number;
}

export interface Ladder {
  by_level: Record<string, number>;
  cells: LadderCell[];
  distinct_cells: number;
  global_fallbacks: number;
}

export interface HarmUplift {
  n: number;
  min_bps: number;
  max_bps: number;
  mean_bps: number;
  positive: number;
  above_fold_threshold: number;
}

export interface FoldThresholds {
  fold: number;
  self_recovery_ceiling_bps: number;
  low_tertile_bps: number;
  high_tertile_bps: number;
  harm_threshold_bps: number;
  training_size: number;
  qualifying_units: number;
}

export interface SegmentRow {
  label: string;
  n: number;
  counts: QuadrantCounts;
  modal_quadrant: Quadrant;
  mean_uplift_bps: number;
  mean_harm_uplift_bps: number;
}

export interface ConfusionMatrix {
  quadrants: Quadrant[];
  strata: SegmentRow[];
  empty_quadrants: Quadrant[];
}

export interface AcceptanceCriterion {
  name: string;
  expected: string;
  observed: string;
  passed: boolean;
}

export interface Acceptance {
  criteria: AcceptanceCriterion[];
  accepted: boolean;
}

export interface Uplift {
  model: UpliftModel;
  qini: Qini;
  top_capture: Capture;
  top_amount_capture: Capture;
  quadrant_counts: QuadrantCounts;
  rule_counts: Record<string, number>;
  gray_zone: GrayZone;
  ladder: Ladder;
  harm_uplift: HarmUplift;
  fold_thresholds: FoldThresholds[];
  confusion_matrix: ConfusionMatrix;
  acceptance: Acceptance;
  limitations: string[];
}

// -- the two payload shapes ------------------------------------------------

/** A complete evaluation report. */
export interface AvailableReport {
  available?: undefined;
  label: string;
  experiment: Experiment;
  power: Power;
  recovery: RateEffect;
  harm: RateEffect;
  ledger: Ledger;
  per_protocol: PerProtocol;
  balance: Balance;
  ground_truth: GroundTruth;
  ground_truth_by_stratum: StratumTruth[];
  accuracy: Accuracy;
  bootstrap: { seed: number; resamples: number };
  /** `null` when the run did not fit an uplift model. */
  uplift: Uplift | null;
  deferred: DeferredSection[];
  limitations: string[];
}

/**
 * No report, and why.
 *
 * The backend refuses to estimate from a population whose outcomes are not all
 * sealed — analysing a partial population would measure the units that resolved
 * quickly rather than a random sample. There is no report to render, so this
 * shape carries the refusal instead of a zeroed-out one.
 */
export interface UnavailableReport {
  available: false;
  label: string;
  experiment: Pick<
    Experiment,
    | "id"
    | "name"
    | "hypothesis"
    | "primary_metric"
    | "alpha_bps"
    | "power_bps"
    | "mde_bps"
    | "planned_n_per_arm"
    | "holdout_bps"
  >;
  refusal: {
    message: string;
    experiment_id: string;
    missing_outcomes: number;
    unsealed_outcomes: number;
  };
  enrolled: number;
  sealed_outcomes: number;
  uplift: null;
}

/**
 * Either a report or a refusal. Discriminated on `available`, which is present
 * and `false` only on the refusal — so consumers must narrow before touching
 * any report field.
 */
export type ReportPayload = AvailableReport | UnavailableReport;

/** Narrowing helper, so the check reads the same way everywhere. */
export function isAvailable(payload: ReportPayload): payload is AvailableReport {
  return payload.available !== false;
}
