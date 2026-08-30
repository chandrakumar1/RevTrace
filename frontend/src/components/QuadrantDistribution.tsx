import { CountBars } from "@/components/CountBars";
import type { CountBarRow } from "@/components/CountBars";
import type { Quadrant, QuadrantCounts } from "@/types/report";

/** Fixed order, so the five always appear in the same sequence. */
const ORDER: Quadrant[] = [
  "persuadable",
  "sure_thing",
  "lost_cause",
  "sleeping_dog",
  "gray_zone",
];

const LABEL: Record<Quadrant, string> = {
  persuadable: "Persuadable",
  sure_thing: "Sure thing",
  lost_cause: "Lost cause",
  sleeping_dog: "Sleeping dog",
  gray_zone: "Gray zone",
};

const COLOR: Record<Quadrant, string> = {
  persuadable: "bg-incremental",
  sure_thing: "bg-neutral",
  lost_cause: "bg-neutral",
  sleeping_dog: "bg-danger",
  gray_zone: "bg-line",
};

const MEANING: Record<Quadrant, string> = {
  persuadable: "acting adds value — the only label that means act",
  sure_thing: "would have paid anyway",
  lost_cause: "was never going to pay",
  sleeping_dog: "acting destroys value",
  gray_zone: "not enough evidence to say",
};

/**
 * The five quadrants, always all five.
 *
 * An empty quadrant is shown at zero and marked, never omitted. On the accepted
 * run SURE_THING and LOST_CAUSE are both empty, and a reader has to be able to
 * tell that apart from a quadrant that was never evaluated.
 */
export function QuadrantDistribution({
  counts,
  emptyQuadrants,
  total,
}: {
  counts: QuadrantCounts;
  emptyQuadrants: Quadrant[];
  total: number;
}) {
  const empty = new Set(emptyQuadrants);

  const rows: CountBarRow[] = ORDER.map((quadrant) => ({
    key: quadrant,
    label: LABEL[quadrant],
    count: counts[quadrant],
    color: COLOR[quadrant],
    muted: empty.has(quadrant),
  }));

  return (
    <div className="space-y-4">
      <CountBars rows={rows} total={total} />

      <dl className="grid gap-x-6 gap-y-1 text-xs text-faint sm:grid-cols-2">
        {ORDER.map((quadrant) => (
          <div key={quadrant} className="flex gap-2">
            <dt className="shrink-0 font-medium text-muted">{LABEL[quadrant]}</dt>
            <dd>{MEANING[quadrant]}</dd>
          </div>
        ))}
      </dl>

      {emptyQuadrants.length > 0 ? (
        <p className="rounded-md border border-line bg-surface px-3 py-2 text-xs leading-relaxed text-muted">
          <strong className="font-semibold text-ink">
            Empty: {emptyQuadrants.map((q) => LABEL[q]).join(", ")}.
          </strong>{" "}
          Listed rather than dropped — nothing landed there, but they were still
          evaluated. Both are defined by a confidence interval that contains zero, and
          at this sample size no cell&rsquo;s interval does. See the limitations below.
        </p>
      ) : null}
    </div>
  );
}
