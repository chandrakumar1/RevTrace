import { formatBps, formatCount } from "@/lib/format";
import type { ConfusionMatrix as Matrix, Quadrant } from "@/types/report";

const SHORT: Record<Quadrant, string> = {
  persuadable: "Persuadable",
  sure_thing: "Sure thing",
  lost_cause: "Lost cause",
  sleeping_dog: "Sleeping dog",
  gray_zone: "Gray zone",
};

/**
 * Assigned quadrant against planted stratum.
 *
 * Only possible because the data is synthetic — the labels being counted came
 * from a model that saw observed features alone, and the comparison happens
 * strictly afterwards.
 *
 * The table scrolls inside its own container rather than widening the page.
 * Seven strata by five quadrants does not fit a phone, and a page that scrolls
 * sideways is worse than a table that does.
 */
export function ConfusionMatrix({ matrix }: { matrix: Matrix }) {
  const empty = new Set(matrix.empty_quadrants);

  return (
    <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
      <table className="w-full min-w-[46rem] border-collapse text-sm">
        <caption className="sr-only">
          Assigned quadrant counts for each planted stratum
        </caption>
        <thead>
          <tr className="border-b border-line text-left">
            <th scope="col" className="py-2 pr-4 font-medium text-muted">
              Planted stratum
            </th>
            <th scope="col" className="py-2 pr-4 text-right font-medium text-muted">
              n
            </th>
            {matrix.quadrants.map((q) => (
              <th
                key={q}
                scope="col"
                className={[
                  "py-2 pr-4 text-right font-medium",
                  empty.has(q) ? "text-faint" : "text-muted",
                ].join(" ")}
              >
                {SHORT[q]}
              </th>
            ))}
            <th scope="col" className="py-2 pr-4 font-medium text-muted">
              Modal
            </th>
            <th scope="col" className="py-2 text-right font-medium text-muted">
              Mean uplift
            </th>
          </tr>
        </thead>
        <tbody>
          {matrix.strata.map((row) => (
            <tr key={row.label} className="border-b border-line/60 last:border-b-0">
              <th scope="row" className="py-2 pr-4 text-left font-normal text-ink">
                <code className="text-xs">{row.label}</code>
              </th>
              <td className="tnum py-2 pr-4 text-right text-muted">
                {formatCount(row.n)}
              </td>
              {matrix.quadrants.map((q) => {
                const value = row.counts[q];
                const isModal = row.modal_quadrant === q;
                return (
                  <td
                    key={q}
                    className={[
                      "tnum py-2 pr-4 text-right",
                      value === 0 ? "text-faint" : "text-ink",
                      isModal ? "font-semibold" : "",
                    ].join(" ")}
                  >
                    {formatCount(value)}
                  </td>
                );
              })}
              <td className="py-2 pr-4">
                <span
                  className={[
                    "rounded px-1.5 py-0.5 text-xs font-medium",
                    row.modal_quadrant === "persuadable"
                      ? "bg-incremental/12 text-incremental"
                      : row.modal_quadrant === "sleeping_dog"
                        ? "bg-danger/12 text-danger"
                        : "bg-line/60 text-muted",
                  ].join(" ")}
                >
                  {SHORT[row.modal_quadrant]}
                </span>
              </td>
              <td className="tnum py-2 text-right font-medium text-ink">
                {formatBps(row.mean_uplift_bps)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
