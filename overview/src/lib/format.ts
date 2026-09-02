/**
 * Display formatting for integer money and integer rates. Formatting only.
 *
 * **No floating-point arithmetic.** Minor units and basis points are both
 * hundredths, so the split into whole and fractional parts is done on the
 * decimal *string* by slicing — `1564` becomes `"15"` and `"64"`, never
 * `1564 / 100`. That mirrors the source project, where floats are banned from
 * money and probability paths outright, and it means the figures on this site
 * are the artifact's integers rendered rather than re-derived.
 *
 * Nothing here computes a business value. Every number reaching these functions
 * was calculated upstream and is only being turned into a string.
 */

/** What a null-valued quantity renders as. Never "0", never "undefined". */
export const ABSENT = "not recorded";

function splitHundredths(value: number): { sign: string; whole: string; frac: string } {
  const sign = value < 0 ? "-" : "";
  const digits = String(Math.abs(value)).padStart(3, "0");
  return { sign, whole: digits.slice(0, -2), frac: digits.slice(-2) };
}

/** Thousands separators. Western grouping, matching the source artifacts. */
function group(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** Basis points as a percentage. `1564` → `"15.64%"`. Sign is preserved. */
export function formatBps(bps: number): string {
  const { sign, whole, frac } = splitHundredths(bps);
  return `${sign}${group(whole)}.${frac}%`;
}

/** Minor units as rupees. `1335458093` → `"₹13,354,580.93"`. */
export function formatMinor(minor: number): string {
  const { sign, whole, frac } = splitHundredths(minor);
  return `${sign}₹${group(whole)}.${frac}`;
}

/**
 * No crore helper, deliberately.
 *
 * An earlier draft showed "≈ ₹1.33 crore" beside each exact figure. It is gone:
 * a rounded second rendering of the same money is one more number to get wrong,
 * and the exact value is the one that can be checked against the artifact.
 */

/** A plain count with separators. `10000` → `"10,000"`. */
export function formatCount(n: number): string {
  const sign = n < 0 ? "-" : "";
  return `${sign}${group(String(Math.abs(n)))}`;
}

/** A basis-point interval. `[1370, 1757]` → `"[13.70%, 17.57%]"`. */
export function formatBpsInterval(low: number, high: number): string {
  return `[${formatBps(low)}, ${formatBps(high)}]`;
}

/**
 * A value that may not exist.
 *
 * The whole point: `null` becomes a phrase, never `undefined` and never `0`.
 * A missing measurement is a fact about the record, not a quantity.
 */
export function formatMaybe(
  value: number | null,
  render: (n: number) => string,
): string {
  return value === null ? ABSENT : render(value);
}
