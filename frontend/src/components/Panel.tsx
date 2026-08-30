import type { ReactNode } from "react";

/** A titled section with an optional explanatory subtitle. */
export function Panel({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-line bg-card p-4 shadow-xs sm:p-6">
      <header className="mb-4">
        <h2 className="text-[0.7rem] font-semibold uppercase tracking-widest text-muted">
          {title}
        </h2>
        {subtitle ? (
          <p className="mt-1.5 max-w-prose text-xs leading-relaxed text-faint">{subtitle}</p>
        ) : null}
      </header>
      {children}
    </section>
  );
}

/** One term/value pair in a definition list. */
export function DefinitionRow({
  term,
  children,
}: {
  term: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-6 border-b border-line py-2 last:border-b-0">
      <dt className="text-sm text-muted">{term}</dt>
      <dd className="tnum text-right text-sm font-medium break-words text-ink">{children}</dd>
    </div>
  );
}
