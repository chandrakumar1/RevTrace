/**
 * The gate comparison artifact's shape.
 *
 * Mirrors `docs/gate_comparison.json`, which is **decisions and spend only**.
 * Causal estimates live in `docs/evaluation.json` and are kept separate on
 * purpose: an action count is not an effect, and putting the two in one file
 * invites a reader to treat a spending reduction as a causal result.
 *
 * Money is an integer count of minor units. Rates are integer basis points.
 */

/** Every abstention reason the gate recorded, by name. */
export interface AbstentionReasons {
  readonly self_recovery_likely: number;
  readonly sleeping_dog: number;
  readonly uplift_not_significant: number;
}

/** How one quadrant's units were disposed of. */
export interface QuadrantDisposition {
  readonly abstained: number;
  readonly explored: number;
  readonly ordinary_acted: number;
  readonly total: number;
}

export interface GateComparison {
  /** "SYNTHETIC / DEMO EVALUATION — …". Displayed, never assumed. */
  readonly label: string;
  readonly note: string;
  readonly experiment_id: string;
  readonly seed: number;
  readonly case_count: number;
  readonly treatment_units: number;
  readonly intervention: string;

  /** Cost of one intervention, in integer minor units. */
  readonly unit_cost: number;

  /** Acting on every treatment unit — the no-gate counterfactual. */
  readonly gate_off: { readonly acted: number; readonly abstained: number };
  readonly gate_off_cost_minor: number;

  /** The gate as it now stands. `acted` = `ordinary_acted` + `explored`. */
  readonly gate_on: {
    readonly acted: number;
    readonly abstained: number;
    readonly explored: number;
    readonly ordinary_acted: number;
  };
  readonly gate_on_cost_minor: number;

  readonly action_reduction: number;
  readonly action_reduction_bps: number;
  readonly abstention_increase: number;
  readonly abstention_reasons: AbstentionReasons;
  readonly cost_avoided_minor: number;

  readonly exploration_budget_bps: number;
  readonly gray_zone: QuadrantDisposition & {
    readonly self_recovery_likely: number;
    readonly uplift_not_significant: number;
  };
  readonly by_quadrant: {
    readonly gray_zone: QuadrantDisposition;
    readonly lost_cause: QuadrantDisposition;
    readonly persuadable: QuadrantDisposition;
    readonly sleeping_dog: QuadrantDisposition;
    readonly sure_thing: QuadrantDisposition;
  };

  readonly provenance: {
    readonly alpha_bps: number;
    readonly as_of: string;
    readonly bootstrap_seed: number;
    readonly folds: number;
    readonly gray_zone_policy: string;
    readonly mde_bps: number;
    readonly migration: string;
    readonly policy_revision: string;
    readonly resamples: number;
  };
}
