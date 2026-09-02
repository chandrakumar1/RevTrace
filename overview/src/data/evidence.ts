/**
 * Every measured figure this site displays. Nothing else may state a number.
 *
 * These values are transcribed from the RevTrace project's own generated
 * artifacts — `docs/evaluation.json` and `docs/gate_comparison.json` — and each
 * one carries the artifact path it came from. This site is a **separate,
 * independent project**: it does not import from the RevTrace application, does
 * not call its backend, and does not read its files at build time. The values
 * are copied deliberately and are stated here once so a reader can check them
 * against the source.
 *
 * Three rules hold, and they are the reason this file exists at all:
 *
 * **Nothing is invented.** There is no figure here that is not in an artifact.
 * No savings estimate, no security score, no attack count, no adoption metric,
 * no testimonial. If a number is not below, the site does not claim it.
 *
 * **Absent is not zero.** A quantity that was never persisted is `null` and is
 * rendered as a sentence explaining its absence — never as `undefined`, never
 * as `0`, and never as a plausible-looking placeholder.
 *
 * **Every figure is synthetic.** The population is generated with planted
 * effects. Recovering a planted effect validates the estimator, not the world,
 * and no part of this describes a real customer or a real payment.
 *
 * Money is an integer count of **minor units (paise)**. Rates and effects are
 * integer **basis points** out of 10,000. Both are formatted for display by
 * `lib/format.ts`, which slices decimal strings rather than dividing — the same
 * convention the source project uses, so a float never enters a money path.
 */

/** Where a figure came from, carried alongside it rather than in a comment. */
export interface Source {
  readonly artifact: string;
  readonly path: string;
}

const EVALUATION = "docs/evaluation.json";
const GATE = "docs/gate_comparison.json";

const src = (artifact: string, path: string): Source => ({ artifact, path });

/* ------------------------------------------------------------------ *
 * Provenance
 * ------------------------------------------------------------------ */

export const provenance = {
  /** The artifact's own label. Not a phrase written for this site. */
  label: "SYNTHETIC / DEMO EVALUATION",
  experimentName: "BENCH-seed42-n10000",
  experimentId: "2b3e9c9f-60e8-5413-adc2-456b89e017b1",
  /** Frozen before the run — a pre-registration, not a post-hoc description. */
  hypothesis:
    "Creating a payment link on a repeated payment failure increases the " +
    "probability of payment within 72 hours, relative to an untreated holdout.",
  lockedAt: "2026-01-01T03:30:00+05:30",
  seed: 42,
  bootstrapSeed: 20260830,
  resamples: 10000,
  source: src(EVALUATION, "label · experiment · bootstrap"),
} as const;

/* ------------------------------------------------------------------ *
 * 02 — The experiment
 * ------------------------------------------------------------------ */

export const experiment = {
  totalUnits: 10000,
  treatment: 5044,
  holdout: 4956,
  /** 50.00% — the pre-registered holdout share, in basis points. */
  holdoutBps: 5000,
  primaryMetric: "recovery_rate",
  source: src(EVALUATION, "ledger.n_treatment · ledger.n_holdout · experiment"),
} as const;

/* ------------------------------------------------------------------ *
 * 03 — The ledger
 * ------------------------------------------------------------------ */

/**
 * The three quantities, in integer paise.
 *
 * They satisfy `gross = incremental + creditedNotEarned` exactly, which is
 * checked below rather than asserted — an identity that silently stopped
 * holding would otherwise be invisible.
 */
export const ledger = {
  grossRecoveredMinor: 1335458093,
  incrementalRecoveredMinor: 447880605,
  creditedNotEarnedMinor: 887577488,
  /** 66.46% of gross was credited but not earned. */
  creditedShareBps: 6646,
  source: src(EVALUATION, "ledger"),
} as const;

/** The identity the ledger rests on. Exported so a test or a reader can see it. */
export const ledgerIdentityHolds =
  ledger.incrementalRecoveredMinor + ledger.creditedNotEarnedMinor ===
  ledger.grossRecoveredMinor;

/* ------------------------------------------------------------------ *
 * 04 — The evidence
 * ------------------------------------------------------------------ */

/**
 * The estimated effect, and the interval around it.
 *
 * **No confidence level is asserted here.** The artifact reports an interval
 * and the bootstrap that produced it; this site reports the same and does not
 * add a claim the source does not make.
 *
 * `trueAteBps` is the **planted** effect — knowable only because the population
 * was generated. It is kept in a separate field from the estimate, and the two
 * are never combined, because conflating them is the single easiest way to make
 * a synthetic result look like a real one.
 */
export const effect = {
  ateBps: 1564,
  ciLowBps: 1370,
  ciHighBps: 1757,
  /** Planted ground truth. Not an estimate, and not available in production. */
  trueAteBps: 1409,
  source: src(EVALUATION, "recovery.ate_bps · recovery.ate_ci_* · accuracy.true_ate_bps"),
} as const;

/* ------------------------------------------------------------------ *
 * 05 — Abstention
 * ------------------------------------------------------------------ */

/**
 * What the gate decided, against acting on everything.
 *
 * Counts only. **No monetary saving is stated on this site**: the source
 * artifact does carry a cost figure, but it rests on a unit cost that is itself
 * a demo assumption, and presenting it as a saving would be the kind of derived
 * claim this project refuses.
 */
export const gate = {
  gateOffActed: 5044,
  gateOnActed: 2450,
  gateOnAbstained: 2594,
  grayZoneTotal: 1879,
  source: src(GATE, "gate_off.acted · gate_on.acted · gate_on.abstained · gray_zone.total"),
} as const;

/* ------------------------------------------------------------------ *
 * 06 — The AI boundary
 * ------------------------------------------------------------------ */

/**
 * The one live model interaction, and what did not survive it.
 *
 * The classification below is what is known. The numeric detail — how many
 * cited integers were checked, and their values — **was never persisted**: the
 * run was rolled back and no audit row exists in any database. Those fields are
 * `null`, and the site renders that absence as a sentence.
 */
export const aiCapture = {
  provider: "OpenRouter",
  model: "nvidia/nemotron-3-super-120b-a12b:free",
  cellKey: "card_declined|card",
  claim: "higher_uplift_than_population",
  rule: "interval_lies_entirely_above_the_population_effect",
  verdict: "confirmed",
  /** Not persisted. Rendered as an explanation, never as a number. */
  citedIntegerCount: null,
  /** The sentence shown wherever the absent figure would have gone. */
  absenceNote:
    "The run was rolled back, so no persisted audit record exists. " +
    "The classification survives; the numeric detail does not.",
  source: src("not persisted", "run_hypothesis.py rolls back by default"),
} as const;

/* ------------------------------------------------------------------ *
 * 07 — Trust boundary
 * ------------------------------------------------------------------ */

/**
 * The controls on the webhook path, as capabilities rather than measurements.
 *
 * Deliberately not scored and not counted. There is no "security score" and no
 * attack tally, because neither exists in any artifact.
 */
export const trustControls = [
  {
    id: "signature",
    label: "Signed payload",
    detail:
      "HMAC-SHA256 verified over the exact bytes delivered, before anything parses them, with a timing-safe comparison.",
  },
  {
    id: "ownership",
    label: "Merchant ownership",
    detail:
      "The owning merchant is derived from the signed payment id, never from a caller-supplied parameter.",
  },
  {
    id: "idempotency",
    label: "Replay",
    detail:
      "A repeated delivery is declined by a uniqueness constraint, so a duplicate is an insert that writes nothing.",
  },
  {
    id: "ordering",
    label: "Ordering",
    detail:
      "Events are ordered by the provider's own timestamp, and a payment state moves forward only.",
  },
  {
    id: "tampered",
    label: "Tampered body",
    detail: "Refused. The signature covers the octets, so an altered amount fails verification.",
  },
  {
    id: "foreign",
    label: "Foreign merchant",
    detail:
      "Refused. A second merchant claiming another's payment is rejected rather than silently ignored.",
  },
] as const;

/**
 * The defect that was found and fixed. Narrative, with no figure attached.
 */
export const crossTenantDefect = {
  headline: "A cross-tenant ownership flaw was found and fixed.",
  body:
    "The merchant a webhook belonged to was once read from a query parameter, outside the " +
    "signed bytes. A validly-signed webhook about one merchant's payment could therefore be " +
    "filed under another — pointing at the victim's order and advancing the victim's payment " +
    "attempt. Checking that the merchant existed caught none of it, because the merchant did " +
    "exist; it simply was not the one that owned the payment. Ownership is now derived from " +
    "the signed payment id, and the parameter survives only as an assertion that can cause a " +
    "rejection.",
} as const;

/* ------------------------------------------------------------------ *
 * 08 — Links
 * ------------------------------------------------------------------ */

export const links = {
  app: "https://revtrace-frontend.onrender.com",
  source: "https://github.com/chandrakumar1/RevTrace",
} as const;

/** Shown wherever a figure is. Not dismissible, not decorative. */
export const DISCLOSURE =
  "Synthetic / offline demonstration — no real Razorpay transaction.";
