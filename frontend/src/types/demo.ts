/**
 * The demo endpoint's response shape.
 *
 * Mirrors `app/services/demo/runner.py`. The backend composes every string —
 * titles, explanations, the mapped event names, the final status — and the
 * browser only arranges them. That is the same rule the fixture pages follow
 * for numbers, and it holds here for a stronger reason: this page makes claims
 * about what is synthetic and what is production code, and a claim the frontend
 * invented would be a claim nobody verified.
 *
 * `tone` is presentation, and `"refused"` is the one that matters. It marks a
 * security control that did its job, so a rejection must render as a success.
 * Styling it as an error would invert what step 6 exists to show.
 */

export type DemoTone = "plain" | "verified" | "refused";

export interface DemoFact {
  label: string;
  value: string;
  note: string | null;
  tone: DemoTone;
  /**
   * Money, as an integer count of minor units, when this fact is about an
   * amount. `null` on every other fact.
   *
   * The backend sends the integer and the browser formats it, the same
   * convention the fixture pages follow. A formatted string on the wire would
   * mean two money renderers; an amount the frontend derived would mean one
   * nobody checked.
   */
  minor: number | null;
}

export interface DemoStep {
  number: number;
  title: string;
  subtitle: string;
  tone: DemoTone;
  facts: DemoFact[];
}

export interface DemoRun {
  /** "DEMO / SYNTHETIC / OFFLINE — not a Razorpay transaction". */
  provenance: string;
  /** The database the demo ran against. Never a DSN. */
  database: string;
  /** Always false. Sent so the page can display the guarantee, not assert it. */
  committed: boolean;
  /** "Rolled back — nothing persisted." */
  final_status: string;
  steps: DemoStep[];
}

export interface DemoStatus {
  enabled: boolean;
  /** Why the demo is unavailable, in the backend's own words. */
  reason: string | null;
  provenance: string;
}
