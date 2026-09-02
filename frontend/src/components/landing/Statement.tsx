/**
 * Statement-of-account primitives.
 *
 * The landing page is laid out as an audited statement rather than a marketing
 * page, and these are the parts that make it read like one: ruled rows, a
 * numbered section spine, figures right-aligned in a tabular column, and a
 * provenance line under anything that claims to be measured.
 *
 * Three rules hold throughout:
 *
 * **Figures are right-aligned and tabular.** A statement is scanned down its
 * number column; proportional digits that fail to line up read as marketing.
 *
 * **Every claim can carry its own qualification.** `note` is not decoration —
 * a figure shown without saying which claim it supports is the confusion this
 * project exists to remove, so the primitive makes room for it rather than
 * leaving it to a caller's discretion.
 *
 * **Nothing here computes.** These components receive formatted strings. The
 * arithmetic happened in the backend, in integers.
 */

import type { ReactNode } from "react";

/** The masthead: what this document is, and what it is not. */
export function StatementHeader({
  title,
  subtitle,
  meta,
}: {
  title: string;
  subtitle: string;
  meta: { label: string; value: string }[];
}) {
  return (
    <header className="border-b-2 border-ink pb-6">
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">{title}</h1>
      <p className="mt-3 max-w-2xl text-sm leading-relaxed text-muted">{subtitle}</p>
      <dl className="mt-5 grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
        {meta.map((row) => (
          <div key={row.label}>
            <dt className="text-[0.65rem] font-semibold uppercase tracking-widest text-faint">
              {row.label}
            </dt>
            <dd className="tnum mt-0.5 text-xs break-words text-muted">{row.value}</dd>
          </div>
        ))}
      </dl>
    </header>
  );
}

/** One numbered section of the statement. */
export function Section({
  ordinal,
  title,
  lede,
  children,
}: {
  ordinal: string;
  title: string;
  lede?: string;
  children: ReactNode;
}) {
  return (
    <section className="border-t border-line pt-8">
      <div className="mb-5 flex gap-4">
        <span className="tnum shrink-0 pt-0.5 text-[0.7rem] font-semibold tracking-widest text-faint">
          {ordinal}
        </span>
        <div>
          <h2 className="text-lg font-semibold tracking-tight sm:text-xl">{title}</h2>
          {lede ? (
            <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted">{lede}</p>
          ) : null}
        </div>
      </div>
      <div className="sm:pl-12">{children}</div>
    </section>
  );
}

export type RowTone = "plain" | "gross" | "credited" | "incremental" | "muted";

const ROW_VALUE: Record<RowTone, string> = {
  plain: "text-ink",
  gross: "text-ink",
  credited: "text-credited",
  incremental: "text-incremental",
  muted: "text-muted",
};

/**
 * One ruled line of the statement: a description, an optional qualification,
 * and a figure in the number column.
 *
 * `emphasis` draws the single line a reader should leave with. At most one per
 * section — a statement where everything is emphasised has emphasised nothing.
 */
export function LedgerRow({
  label,
  value,
  note,
  tone = "plain",
  emphasis = false,
}: {
  label: string;
  value: string;
  note?: string;
  tone?: RowTone;
  emphasis?: boolean;
}) {
  return (
    <div
      className={[
        "flex items-baseline justify-between gap-6 border-b border-line py-3 last:border-b-0",
        emphasis ? "border-b-ink" : "",
      ].join(" ")}
    >
      <div className="min-w-0">
        <p
          className={[
            "text-sm",
            emphasis ? "font-semibold text-ink" : "text-muted",
          ].join(" ")}
        >
          {label}
        </p>
        {note ? (
          <p className="mt-1 max-w-prose text-xs leading-relaxed text-faint">{note}</p>
        ) : null}
      </div>
      <p
        className={[
          "tnum shrink-0 text-right tabular-nums",
          emphasis ? "text-xl font-semibold sm:text-2xl" : "text-sm font-medium",
          ROW_VALUE[tone],
        ].join(" ")}
      >
        {value}
      </p>
    </div>
  );
}

/** A closing rule with a totalling line, as a statement subtotal. */
export function Subtotal({
  label,
  value,
  note,
  tone = "incremental",
}: {
  label: string;
  value: string;
  note?: string;
  tone?: RowTone;
}) {
  return (
    <div className="mt-1 border-t-2 border-ink pt-3">
      <div className="flex items-baseline justify-between gap-6">
        <p className="text-sm font-semibold">{label}</p>
        <p className={`tnum text-right text-2xl font-semibold tabular-nums ${ROW_VALUE[tone]}`}>
          {value}
        </p>
      </div>
      {note ? (
        <p className="mt-1.5 max-w-prose text-xs leading-relaxed text-faint">{note}</p>
      ) : null}
    </div>
  );
}

/**
 * The line under a measured claim saying where it came from.
 *
 * Present on every section that reports a figure. A statement without
 * provenance is an assertion.
 */
export function Provenance({ children }: { children: ReactNode }) {
  return (
    <p className="mt-4 border-l-2 border-line pl-3 text-xs leading-relaxed text-faint">
      {children}
    </p>
  );
}

/**
 * A two-column comparison, for a counterfactual: what happened against what
 * would have happened.
 */
export function Comparison({
  left,
  right,
}: {
  left: { heading: string; rows: { label: string; value: string }[]; note?: string };
  right: { heading: string; rows: { label: string; value: string }[]; note?: string };
}) {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {[left, right].map((col, index) => (
        <div
          key={col.heading}
          className={[
            "rounded-md border p-4",
            index === 1 ? "border-incremental/40 bg-incremental/5" : "border-line bg-card",
          ].join(" ")}
        >
          <h3 className="text-[0.65rem] font-semibold uppercase tracking-widest text-muted">
            {col.heading}
          </h3>
          <dl className="mt-3">
            {col.rows.map((row) => (
              <div
                key={row.label}
                className="flex items-baseline justify-between gap-4 border-b border-line py-1.5 last:border-b-0"
              >
                <dt className="text-xs text-muted">{row.label}</dt>
                <dd className="tnum text-right text-sm font-medium tabular-nums">{row.value}</dd>
              </div>
            ))}
          </dl>
          {col.note ? (
            <p className="mt-3 text-xs leading-relaxed text-faint">{col.note}</p>
          ) : null}
        </div>
      ))}
    </div>
  );
}

/** A prose block for a section that reports no figure. */
export function Prose({ children }: { children: ReactNode }) {
  return <div className="max-w-2xl space-y-3 text-sm leading-relaxed text-muted">{children}</div>;
}
