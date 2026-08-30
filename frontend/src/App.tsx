/**
 * Shell: page selection, fixture selection, the synthetic label.
 *
 * There is no API layer. The app reads committed fixtures at build time; wiring
 * to a live endpoint is a later phase and no endpoint shape has been agreed.
 */

import { useState } from "react";

import { FixturePicker } from "@/components/FixturePicker";
import { SyntheticBanner } from "@/components/SyntheticBanner";
import { DEFAULT_FIXTURE_ID, fixtureById } from "@/fixtures";
import { EvaluationPage, EvaluationRefusal } from "@/pages/EvaluationPage";
import { LedgerPage, LedgerRefusal } from "@/pages/LedgerPage";
import { isAvailable } from "@/types/report";

const PAGES = [
  {
    id: "ledger",
    ordinal: "Page 1",
    title: "Incrementality ledger",
    blurb:
      "How much of the recovered revenue this system actually caused — and how much would have arrived without it.",
  },
  {
    id: "evaluation",
    ordinal: "Page 2",
    title: "Evaluation",
    blurb:
      "What the uplift model measured, and what these numbers do not establish.",
  },
] as const;

type PageId = (typeof PAGES)[number]["id"];

export default function App() {
  const [pageId, setPageId] = useState<PageId>("ledger");
  const [fixtureId, setFixtureId] = useState(DEFAULT_FIXTURE_ID);

  const fixture = fixtureById(fixtureId);
  const payload = fixture.payload;
  const page = PAGES.find((p) => p.id === pageId) ?? PAGES[0];

  return (
    <div className="min-h-dvh bg-surface">
      <div className="mx-auto max-w-6xl px-4 py-8 sm:px-6 sm:py-10">
        <header className="mb-6 border-b border-line pb-6">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <p className="text-[0.7rem] font-semibold uppercase tracking-widest text-faint">
                {page.ordinal}
              </p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight sm:text-3xl">
                {page.title}
              </h1>
              <p className="mt-2 max-w-prose text-sm leading-relaxed text-muted">
                {page.blurb}
              </p>
            </div>
            <SyntheticBanner label={payload.label} />
          </div>

          <nav className="mt-5 flex gap-1" aria-label="Pages">
            {PAGES.map((entry) => {
              const active = entry.id === pageId;
              return (
                <button
                  key={entry.id}
                  type="button"
                  onClick={() => setPageId(entry.id)}
                  aria-current={active ? "page" : undefined}
                  className={[
                    "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                    active
                      ? "bg-ink text-surface"
                      : "text-muted hover:bg-line/50 hover:text-ink",
                  ].join(" ")}
                >
                  {entry.title}
                </button>
              );
            })}
          </nav>
        </header>

        <div className="mb-8">
          <FixturePicker
            value={fixtureId}
            onChange={setFixtureId}
            description={fixture.description}
          />
        </div>

        {isAvailable(payload) ? (
          pageId === "ledger" ? (
            <LedgerPage report={payload} />
          ) : (
            <EvaluationPage report={payload} />
          )
        ) : pageId === "ledger" ? (
          <LedgerRefusal report={payload} />
        ) : (
          <EvaluationRefusal report={payload} />
        )}

        <footer className="mt-10 border-t border-line pt-6 text-xs leading-relaxed text-faint">
          Money is carried as integer minor units and rates as integer basis points; the
          browser formats them and computes nothing. Reading a committed fixture — no API is
          wired.
        </footer>
      </div>
    </div>
  );
}
