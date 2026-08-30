import { formatBps, formatCount, formatMinor, formatOptionalBps } from "@/lib/format";
import { shareOf } from "@/lib/geometry";
import type { Capture } from "@/types/report";

/**
 * Top-share capture, by unit count and by money, side by side.
 *
 * These answer different questions and routinely disagree. A ranking that
 * promotes many cheap recoveries scores well by count and badly by amount, and
 * only the second is revenue — so they are shown together, at equal weight,
 * with the count figure explicitly marked as not a revenue claim.
 *
 * `capture_bps` is `null` when there was no incremental recovery to apportion.
 * That is rendered as unavailable, never as zero.
 */
function CaptureRow({
  heading,
  caption,
  capture,
  unit,
  accent,
}: {
  heading: string;
  caption: string;
  capture: Capture;
  unit: "count" | "amount";
  accent: string;
}) {
  const format = unit === "amount" ? formatMinor : formatCount;
  const width = capture.capture_bps === null ? 0 : shareOf(capture.capture_bps, 10_000);

  return (
    <div className="rounded-lg border border-line bg-card p-4">
      <h3 className="text-[0.7rem] font-semibold uppercase tracking-widest text-muted">
        {heading}
      </h3>
      <p className={`tnum mt-2 text-2xl font-semibold ${accent}`}>
        {formatOptionalBps(capture.capture_bps)}
      </p>

      <div className="mt-3 h-2 overflow-hidden rounded bg-line/40">
        <div className={`h-full rounded ${accent.replace("text-", "bg-")}`} style={{ width: `${width}%` }} />
      </div>

      <dl className="mt-3 space-y-1 text-xs">
        <div className="flex justify-between gap-4">
          <dt className="text-muted">Captured in top {formatBps(capture.share_bps)}</dt>
          <dd className="tnum text-ink">{format(capture.qini_at_k)}</dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-muted">Total incremental</dt>
          <dd className="tnum text-ink">{format(capture.total)}</dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-muted">Units in prefix</dt>
          <dd className="tnum text-ink">
            {formatCount(capture.k)} of {formatCount(capture.n)}
          </dd>
        </div>
      </dl>

      <p className="mt-3 text-xs leading-relaxed text-faint">{caption}</p>
    </div>
  );
}

export function CaptureComparison({
  byCount,
  byAmount,
}: {
  byCount: Capture;
  byAmount: Capture;
}) {
  return (
    <div className="space-y-3">
      <div className="grid gap-4 md:grid-cols-2">
        <CaptureRow
          heading="By unit count"
          caption="How many recoveries the top of the ranking captured. Not a revenue claim — recoveries are not equal in value."
          capture={byCount}
          unit="count"
          accent="text-neutral"
        />
        <CaptureRow
          heading="By recovered amount"
          caption="How much incremental money the same prefix captured. This is the figure to quote when the claim is about revenue."
          capture={byAmount}
          unit="amount"
          accent="text-incremental"
        />
      </div>
      <p className="rounded-md border border-line bg-surface px-3 py-2 text-xs leading-relaxed text-muted">
        The two are deliberately not combined. Where they disagree, the ranking is
        better at finding recoveries than at finding valuable ones, and only the
        amount-weighted figure supports a statement about revenue.
      </p>
    </div>
  );
}
