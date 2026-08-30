import { formatMinor, formatMinorInterval } from "@/lib/format";

/**
 * Gross → credited-not-earned → incremental, as a waterfall.
 *
 * The shape carries the argument: start at what a recovery dashboard would
 * report, remove the part that would have arrived anyway, and land on the only
 * figure the system can claim to have caused.
 *
 * **Bar widths are layout geometry, not reported figures.** `shareOf` converts
 * an amount into a percentage of the total purely to size a `<div>`; its result
 * is never displayed, and no number on screen is derived from it. Every visible
 * figure is a fixture value passed through a formatter — nothing here computes
 * a difference, a share, or an interval.
 */
function shareOf(part: number, total: number): number {
  if (total <= 0) {
    return 0;
  }
  // Integer basis points first, so the ratio is exact for the magnitudes money
  // arrives in; the final divide exists only to produce a CSS percentage.
  const bps = Math.round((Math.abs(part) * 10_000) / total);
  return Math.min(bps, 10_000) / 100;
}

interface Row {
  label: string;
  amount: number;
  /** Distance from the left edge, as a percentage. */
  offset: number;
  width: number;
  color: string;
  prefix?: string;
  interval?: { low: number; high: number };
  emphasis?: boolean;
}

export function Waterfall({
  gross,
  creditedNotEarned,
  incremental,
  incrementalCiLow,
  incrementalCiHigh,
}: {
  gross: number;
  creditedNotEarned: number;
  incremental: number;
  incrementalCiLow: number;
  incrementalCiHigh: number;
}) {
  const incrementalWidth = shareOf(incremental, gross);

  const rows: Row[] = [
    {
      label: "Gross recovered",
      amount: gross,
      offset: 0,
      width: shareOf(gross, gross),
      color: "bg-gross",
    },
    {
      label: "Credited-not-earned",
      amount: creditedNotEarned,
      // Right-aligned against gross, so it reads as the slice being taken away.
      offset: incrementalWidth,
      width: shareOf(creditedNotEarned, gross),
      color: "bg-credited",
      prefix: "−",
    },
    {
      label: "Incremental recovered",
      amount: incremental,
      offset: 0,
      width: incrementalWidth,
      color: "bg-incremental",
      prefix: "=",
      interval: { low: incrementalCiLow, high: incrementalCiHigh },
      emphasis: true,
    },
  ];

  return (
    <div className="space-y-4">
      {rows.map((row) => (
        <div key={row.label} className="grid gap-1.5 sm:grid-cols-[13rem_1fr] sm:items-center sm:gap-4">
          <div className="flex items-baseline gap-2 sm:block">
            <span
              className={[
                "text-sm",
                row.emphasis ? "font-semibold text-ink" : "text-muted",
              ].join(" ")}
            >
              {row.prefix ? (
                <span aria-hidden className="mr-1 text-faint">
                  {row.prefix}
                </span>
              ) : null}
              {row.label}
            </span>
          </div>

          <div>
            <div className="relative h-7 w-full overflow-hidden rounded bg-line/40">
              <div
                className={`absolute inset-y-0 rounded ${row.color}`}
                style={{ left: `${row.offset}%`, width: `${row.width}%` }}
              />
            </div>
            <p
              className={[
                "tnum mt-1 text-sm",
                row.emphasis ? "font-semibold text-incremental" : "text-ink",
              ].join(" ")}
            >
              {formatMinor(row.amount)}
              {row.interval ? (
                <span className="ml-2 font-normal text-muted">
                  95% CI {formatMinorInterval(row.interval.low, row.interval.high)}
                </span>
              ) : null}
            </p>
          </div>
        </div>
      ))}
    </div>
  );
}
