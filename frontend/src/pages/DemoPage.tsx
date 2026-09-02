/**
 * Page 3 — the live recovery demo.
 *
 * The other two pages show what was *measured*. This one shows what the system
 * *does*: a payment fails, a recovery link is built, signed webhooks arrive,
 * they are verified and attributed, a replay changes nothing, and two attacks
 * are refused.
 *
 * Every word describing what is real and what is synthetic comes from the
 * backend. That is not laziness about copy — this page makes claims about which
 * code path ran, and a claim composed in the browser would be one nobody
 * verified. The frontend arranges strings and formats one integer.
 *
 * **Step 6 renders green.** A refused webhook is a control working, and showing
 * it in red would tell a viewer the demo failed at exactly the moment it
 * succeeded most. That inversion is the single most important styling decision
 * on the page.
 */

import { useEffect, useState } from "react";

import { Panel } from "@/components/Panel";
import { DemoApiError, fetchDemoStatus, runDemo } from "@/lib/api";
import { formatMinor } from "@/lib/format";
import type { DemoFact, DemoRun, DemoStatus, DemoStep, DemoTone } from "@/types/demo";

/** Border and accent per tone. `refused` is deliberately the confident colour. */
const TONE_BORDER: Record<DemoTone, string> = {
  plain: "border-line",
  verified: "border-incremental/40",
  refused: "border-incremental/40",
};

const TONE_ACCENT: Record<DemoTone, string> = {
  plain: "bg-line",
  verified: "bg-incremental",
  refused: "bg-incremental",
};

const TONE_TEXT: Record<DemoTone, string> = {
  plain: "text-ink",
  verified: "text-incremental",
  refused: "text-incremental",
};

/** The mark beside a fact. A refusal is a tick, because it is a success. */
const TONE_MARK: Record<DemoTone, string> = {
  plain: "·",
  verified: "✓",
  refused: "✓",
};

function FactRow({ fact }: { fact: DemoFact }) {
  return (
    <li className="border-b border-line py-2.5 last:border-b-0">
      <div className="flex items-baseline justify-between gap-4">
        <span className="text-sm text-muted">{fact.label}</span>
        <span
          className={`tnum text-right text-sm font-medium break-words ${TONE_TEXT[fact.tone]}`}
        >
          <span aria-hidden className="mr-1.5 font-normal">
            {TONE_MARK[fact.tone]}
          </span>
          {/* Money is formatted here from the integer the backend sent, never
              recomputed and never parsed out of `value`. */}
          {fact.minor === null ? fact.value : formatMinor(fact.minor)}
        </span>
      </div>
      {fact.note ? (
        <p className="mt-1 max-w-prose text-xs leading-relaxed text-faint">{fact.note}</p>
      ) : null}
    </li>
  );
}

function StepCard({ step }: { step: DemoStep }) {
  return (
    <article
      className={`relative overflow-hidden rounded-lg border bg-card p-4 shadow-xs sm:p-6 ${TONE_BORDER[step.tone]}`}
    >
      <span aria-hidden className={`absolute inset-x-0 top-0 h-0.5 ${TONE_ACCENT[step.tone]}`} />
      <header className="mb-3">
        <p className="text-[0.7rem] font-semibold uppercase tracking-widest text-faint">
          Step {step.number}
        </p>
        <h3 className="mt-1 text-base font-semibold tracking-tight">{step.title}</h3>
        <p className="mt-1.5 max-w-prose text-xs leading-relaxed text-muted">{step.subtitle}</p>
      </header>
      <ul>
        {step.facts.map((fact, index) => (
          <FactRow key={`${fact.label}-${index}`} fact={fact} />
        ))}
      </ul>
    </article>
  );
}

/**
 * The provenance banner. Always visible, before and after a run.
 *
 * Not decoration and not dismissible. A screenshot of this page must carry the
 * fact that nothing on it is a Razorpay transaction, without the viewer having
 * to consult documentation to find that out.
 */
function DemoBanner() {
  return (
    <div
      role="note"
      className="rounded-lg border border-synthetic/40 bg-synthetic/10 px-4 py-3"
    >
      <div className="flex items-center gap-2">
        <span aria-hidden className="size-1.5 rounded-full bg-synthetic" />
        <span className="text-xs font-semibold uppercase tracking-widest text-synthetic">
          Demo / Synthetic / Offline
        </span>
      </div>
      <p className="mt-1.5 text-xs leading-relaxed text-muted">
        Not a Razorpay transaction. No credentials required. No external network. The
        payment-link response comes from a deterministic offline demo provider; signature
        verification, merchant attribution, idempotency and status transitions are the
        production verification code path.
      </p>
    </div>
  );
}

export function DemoPage() {
  const [status, setStatus] = useState<DemoStatus | null>(null);
  const [run, setRun] = useState<DemoRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchDemoStatus()
      .then((next) => {
        if (!cancelled) setStatus(next);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setStatus(null);
        setError(
          cause instanceof DemoApiError
            ? cause.message
            : "The backend is not reachable. Start it with `uvicorn app.main:app --reload`.",
        );
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onRun() {
    setRunning(true);
    setError(null);
    try {
      setRun(await runDemo());
    } catch (cause: unknown) {
      setRun(null);
      setError(
        cause instanceof DemoApiError
          ? cause.message
          : "The backend is not reachable. Start it with `uvicorn app.main:app --reload`.",
      );
    } finally {
      setRunning(false);
    }
  }

  const disabled = running || status?.enabled === false;

  return (
    <div className="space-y-6">
      <DemoBanner />

      <Panel
        title="One synthetic recovery, end to end"
        subtitle={
          "A synthetic payment fails, and RevTrace recovers it. The transaction is rolled " +
          "back when the run finishes — there is no option to keep it, and the endpoint " +
          "takes no parameter that would change that."
        }
      >
        <div className="flex flex-wrap items-center gap-4">
          <button
            type="button"
            onClick={() => void onRun()}
            disabled={disabled}
            className={[
              "rounded-md px-4 py-2 text-sm font-semibold transition-colors",
              disabled
                ? "cursor-not-allowed bg-line text-faint"
                : "bg-ink text-surface hover:opacity-90",
            ].join(" ")}
          >
            {running ? "Running…" : "▶ Run Demo"}
          </button>
          {run ? (
            <span className="tnum text-xs text-muted">
              database: {run.database} · committed: {String(run.committed)}
            </span>
          ) : null}
        </div>

        {status?.enabled === false ? (
          <p className="mt-4 max-w-prose rounded-md border border-line bg-surface px-3 py-2 text-xs leading-relaxed text-muted">
            {status.reason}
          </p>
        ) : null}

        {error ? (
          <p
            role="alert"
            className="mt-4 max-w-prose rounded-md border border-danger/40 bg-danger/5 px-3 py-2 text-xs leading-relaxed text-ink"
          >
            {error}
          </p>
        ) : null}
      </Panel>

      {run ? (
        <>
          <div className="space-y-4">
            {run.steps.map((step) => (
              <StepCard key={step.number} step={step} />
            ))}
          </div>

          <div className="rounded-lg border border-incremental/40 bg-card p-4 text-center shadow-xs sm:p-6">
            <p className="text-sm font-semibold text-incremental">{run.final_status}</p>
            <p className="mt-1.5 text-xs leading-relaxed text-faint">{run.provenance}</p>
          </div>
        </>
      ) : null}
    </div>
  );
}
