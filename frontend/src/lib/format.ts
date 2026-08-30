/**
 * Display formatting for backend integers. Formatting only.
 *
 * Nothing here computes a business or statistical value. Every number that
 * reaches these functions was calculated by the backend, in integer arithmetic,
 * and these functions turn it into a string. There is no rounding decision, no
 * unit conversion that loses information, and no derived metric.
 *
 * **No floating-point arithmetic.** Basis points and minor units are both
 * hundredths, so the split into whole and fractional parts is done on the
 * *decimal string*, not by dividing. `1564` becomes `"15"` and `"64"` by
 * slicing, never by `1564 / 100`. That keeps the frontend on the same footing
 * as the backend, where ADR 0001 bans floats from these paths outright.
 *
 * `null` is rendered as "undefined", never as a zero. The backend uses `null`
 * to mean a quantity has no value — an undefined Qini coefficient is not a
 * coefficient of zero — and collapsing the two would turn the absence of a
 * measurement into a measurement.
 */

/** What a null-valued quantity renders as. Not "0", and not blank. */
export const UNDEFINED = "undefined";

/** Split a hundredths integer into its whole and two-digit fractional parts. */
function splitHundredths(value: number): { sign: string; whole: string; frac: string } {
  const sign = value < 0 ? "-" : "";
  const digits = String(Math.abs(value)).padStart(3, "0");
  return {
    sign,
    whole: digits.slice(0, -2),
    frac: digits.slice(-2),
  };
}

/** Thousands separators, applied to a string of digits. */
function group(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/**
 * Basis points as a percentage string. `1564` -> `"15.64%"`.
 *
 * Negative values keep their sign: a sleeping dog's uplift is genuinely below
 * zero and must not be shown as a magnitude.
 */
export function formatBps(bps: number): string {
  const { sign, whole, frac } = splitHundredths(bps);
  return `${sign}${group(whole)}.${frac}%`;
}

/** Basis points, or "undefined" when the backend sent null. */
export function formatOptionalBps(bps: number | null): string {
  return bps === null ? UNDEFINED : formatBps(bps);
}

/**
 * Minor units as rupees. `447880605` -> `"₹4,478,806.05"`.
 *
 * Grouping matches the backend's own rendering in `docs/EVALUATION.md` so the
 * same figure reads identically in both places.
 */
export function formatMinor(minor: number): string {
  const { sign, whole, frac } = splitHundredths(minor);
  return `${sign}₹${group(whole)}.${frac}`;
}

/** A plain count with thousands separators. `10000` -> `"10,000"`. */
export function formatCount(n: number): string {
  const sign = n < 0 ? "-" : "";
  return `${sign}${group(String(Math.abs(n)))}`;
}

/** A basis-point confidence interval. `[1370, 1757]` -> `"[13.70%, 17.57%]"`. */
export function formatBpsInterval(lowBps: number, highBps: number): string {
  return `[${formatBps(lowBps)}, ${formatBps(highBps)}]`;
}

/** A money confidence interval, in minor units. */
export function formatMinorInterval(lowMinor: number, highMinor: number): string {
  return `[${formatMinor(lowMinor)}, ${formatMinor(highMinor)}]`;
}

/**
 * A p-value carried as millionths. `0` -> `"< 0.000001"`.
 *
 * Zero millionths means "smaller than this representation can express", not
 * "exactly zero", and the string says so. Mirrors the backend's own renderer.
 */
export function formatPValueMicros(micros: number): string {
  if (micros === 0) {
    return "< 0.000001";
  }
  const digits = String(micros).padStart(7, "0");
  return `${digits.slice(0, -6)}.${digits.slice(-6)}`;
}

/** An ISO timestamp as sent, or a dash when the backend sent null. */
export function formatTimestamp(iso: string | null): string {
  return iso ?? "—";
}
