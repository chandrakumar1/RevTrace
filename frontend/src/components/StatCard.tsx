import type { ReactNode } from "react";

export type StatTone = "gross" | "credited" | "incremental" | "plain";

const TONE_ACCENT: Record<StatTone, string> = {
  gross: "bg-gross",
  credited: "bg-credited",
  incremental: "bg-incremental",
  plain: "bg-line",
};

const TONE_VALUE: Record<StatTone, string> = {
  gross: "text-ink",
  credited: "text-ink",
  incremental: "text-incremental",
  plain: "text-ink",
};

/**
 * One headline figure, with the sentence that stops it being misread.
 *
 * `note` is not optional decoration. "Gross recovered" and "incremental
 * recovered" are different claims about the same money, and a number shown
 * without saying which one it is invites exactly the confusion this project
 * exists to remove.
 */
export function StatCard({
  label,
  value,
  note,
  detail,
  tone = "plain",
  emphasis = false,
}: {
  label: string;
  value: string;
  note: string;
  detail?: ReactNode;
  tone?: StatTone;
  emphasis?: boolean;
}) {
  return (
    <article
      className={[
        "relative overflow-hidden rounded-lg border bg-card p-4 shadow-xs sm:p-5",
        emphasis ? "border-incremental/40 ring-1 ring-incremental/15" : "border-line",
      ].join(" ")}
    >
      <span aria-hidden className={`absolute inset-x-0 top-0 h-0.5 ${TONE_ACCENT[tone]}`} />
      <h3 className="text-[0.7rem] font-semibold uppercase tracking-widest text-muted">
        {label}
      </h3>
      <p
        className={[
          "tnum mt-2 font-semibold tabular-nums",
          emphasis ? "text-2xl sm:text-3xl" : "text-xl sm:text-2xl",
          TONE_VALUE[tone],
        ].join(" ")}
      >
        {value}
      </p>
      {detail ? <div className="tnum mt-1 text-xs text-muted">{detail}</div> : null}
      <p className="mt-3 text-xs leading-relaxed text-faint">{note}</p>
    </article>
  );
}
