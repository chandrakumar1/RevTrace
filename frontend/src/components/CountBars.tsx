import { formatCount } from "@/lib/format";
import { maxCount, shareOf } from "@/lib/geometry";

export interface CountBarRow {
  key: string;
  label: string;
  count: number;
  /** Tailwind background utility for the bar. */
  color?: string;
  /** Shown to the right of the count. */
  suffix?: string;
  /** Rendered greyed, with an explicit "0" rather than being dropped. */
  muted?: boolean;
}

/**
 * A labelled set of counts as horizontal bars.
 *
 * Zero-valued rows are kept, not filtered out. A category nothing landed in was
 * still evaluated, and dropping it would make it indistinguishable from one
 * that was never considered — which is exactly the mistake the empty
 * SURE_THING and LOST_CAUSE quadrants would invite.
 */
export function CountBars({
  rows,
  total,
}: {
  rows: CountBarRow[];
  total?: number;
}) {
  const scale =
    total ?? maxCount(Object.fromEntries(rows.map((r) => [r.key, r.count])));

  return (
    <ul className="space-y-2.5">
      {rows.map((row) => (
        <li key={row.key} className="grid gap-1 sm:grid-cols-[14rem_1fr] sm:items-center sm:gap-4">
          <span
            className={[
              "truncate text-sm",
              row.muted ? "text-faint" : "text-ink",
            ].join(" ")}
            title={row.label}
          >
            {row.label}
          </span>
          <div className="flex items-center gap-3">
            <div className="relative h-5 min-w-0 flex-1 overflow-hidden rounded bg-line/40">
              <div
                className={`absolute inset-y-0 left-0 rounded ${row.color ?? "bg-neutral"}`}
                style={{ width: `${shareOf(row.count, scale)}%` }}
              />
            </div>
            <span
              className={[
                "tnum w-24 shrink-0 text-right text-sm tabular-nums",
                row.muted ? "text-faint" : "font-medium text-ink",
              ].join(" ")}
            >
              {formatCount(row.count)}
              {row.suffix ? (
                <span className="ml-1 font-normal text-muted">{row.suffix}</span>
              ) : null}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
