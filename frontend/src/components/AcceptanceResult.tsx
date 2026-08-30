import type { Acceptance } from "@/types/report";

/**
 * The acceptance criterion, clause by clause.
 *
 * Reported, not enforced. The backend builds this report whether the clauses
 * hold or not, and a failing clause is a result rather than an error — so a
 * failure renders as plainly as a pass.
 */
export function AcceptanceResult({ acceptance }: { acceptance: Acceptance }) {
  return (
    <div className="space-y-4">
      <div
        className={[
          "flex items-center gap-3 rounded-md border px-4 py-3",
          acceptance.accepted
            ? "border-incremental/40 bg-incremental/8"
            : "border-danger/40 bg-danger/8",
        ].join(" ")}
      >
        <span
          aria-hidden
          className={`size-2 rounded-full ${acceptance.accepted ? "bg-incremental" : "bg-danger"}`}
        />
        <p
          className={[
            "text-sm font-semibold tracking-wide",
            acceptance.accepted ? "text-incremental" : "text-danger",
          ].join(" ")}
        >
          {acceptance.accepted ? "ACCEPTED" : "NOT ACCEPTED"}
        </p>
        <p className="text-xs text-muted">
          {acceptance.criteria.filter((c) => c.passed).length} of{" "}
          {acceptance.criteria.length} clauses hold
        </p>
      </div>

      <ol className="space-y-2">
        {acceptance.criteria.map((criterion) => (
          <li
            key={criterion.name}
            className="rounded-md border border-line bg-card p-3 sm:p-4"
          >
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span
                className={[
                  "rounded px-1.5 py-0.5 text-[0.7rem] font-bold tracking-wider",
                  criterion.passed
                    ? "bg-incremental/12 text-incremental"
                    : "bg-danger/12 text-danger",
                ].join(" ")}
              >
                {criterion.passed ? "PASS" : "FAIL"}
              </span>
              <p className="min-w-0 flex-1 break-words text-sm text-ink">{criterion.name}</p>
            </div>
            <dl className="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
              <div className="flex gap-2">
                <dt className="shrink-0 text-muted">Expected</dt>
                <dd className="break-words text-ink">{criterion.expected}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="shrink-0 text-muted">Observed</dt>
                <dd className="tnum break-words text-ink">{criterion.observed}</dd>
              </div>
            </dl>
          </li>
        ))}
      </ol>
    </div>
  );
}
