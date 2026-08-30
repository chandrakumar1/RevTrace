/**
 * The golden fixtures, typed.
 *
 * These four JSON files are backend output, generated and independently
 * verified as part of the Day 5 closeout. `report.10k.json` is a byte-for-byte
 * copy of `docs/evaluation.json`, the frozen artifact from the accepted
 * N=10,000 run. **Do not hand-edit any of them** — regenerate from the backend
 * instead, or the fixture stops being evidence of anything.
 *
 * There is no API layer. The app reads these files at build time and nothing
 * else; wiring to a live endpoint is a later phase, and no endpoint shape has
 * been agreed yet.
 *
 * The casts are deliberate. TypeScript infers a very wide literal type from an
 * imported JSON file — every string becomes `string`, every array a mutable
 * tuple-ish array — which does not narrow to a discriminated union on its own.
 * The declared shapes in `@/types/report` are the contract; these casts assert
 * the files match it, and a mismatch shows up the moment a field is read.
 */

import type { ReportPayload } from "@/types/report";

import empty from "./report.empty.json";
import tenK from "./report.10k.json";
import qiniUndefined from "./report.qini_undefined.json";
import underpowered from "./report.underpowered.json";

export interface FixtureEntry {
  /** Stable key, used for selection. */
  id: string;
  /** Short human label for the picker. */
  label: string;
  /** What this fixture exists to exercise. */
  description: string;
  payload: ReportPayload;
}

export const FIXTURES: readonly FixtureEntry[] = [
  {
    id: "10k",
    label: "Accepted run (N=10,000)",
    description:
      "The frozen acceptance artifact: seed 42, 5 folds, 10,000 resamples. " +
      "Identical to docs/evaluation.json.",
    payload: tenK as unknown as ReportPayload,
  },
  {
    id: "underpowered",
    label: "Underpowered (N=600)",
    description:
      "298 units per arm against a pre-registered plan of 384, so the report " +
      "carries is_underpowered = true.",
    payload: underpowered as unknown as ReportPayload,
  },
  {
    id: "qini_undefined",
    label: "Qini undefined (N=300)",
    description:
      "Q(N) is exactly zero, so the coefficient and the capture share are " +
      "null — undefined, not zero.",
    payload: qiniUndefined as unknown as ReportPayload,
  },
  {
    id: "empty",
    label: "No sealed outcomes (N=40)",
    description:
      "Every observation window is still open, so the backend refuses to " +
      "estimate. There is no report, only the refusal.",
    payload: empty as unknown as ReportPayload,
  },
] as const;

export const DEFAULT_FIXTURE_ID = "10k";

export function fixtureById(id: string): FixtureEntry {
  const found = FIXTURES.find((entry) => entry.id === id);
  if (!found) {
    throw new Error(`no fixture with id ${id}`);
  }
  return found;
}
