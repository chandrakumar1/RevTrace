/**
 * Layout geometry. Never a reported figure.
 *
 * These helpers size DOM elements. Their output reaches `style` and nothing
 * else — no value produced here is ever displayed, and nothing on screen is
 * derived from one. Every visible number comes from the backend payload through
 * a formatter.
 *
 * `Waterfall.tsx` carries its own copy of this logic. Unifying the two would
 * mean editing a verified Page 1 component, which is not worth the regression
 * risk for a five-line function; worth folding together when Page 1 is next
 * touched for its own reasons.
 */

/** `part` as a percentage of `total`, clamped to 0..100, for a CSS width. */
export function shareOf(part: number, total: number): number {
  if (total <= 0) {
    return 0;
  }
  // Integer basis points first, so the ratio is exact at the magnitudes money
  // and counts arrive in; the final divide only produces a CSS percentage.
  const bps = Math.round((Math.abs(part) * 10_000) / total);
  return Math.min(bps, 10_000) / 100;
}

/** The largest value in a count map, for scaling a set of bars. */
export function maxCount(counts: Record<string, number>): number {
  return Object.values(counts).reduce((a, b) => (b > a ? b : a), 0);
}
