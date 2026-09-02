/**
 * Shell: page selection, fixture selection, the synthetic label.
 *
 * Two kinds of page live here and the difference matters. The ledger and
 * evaluation pages read **committed fixtures** at build time — frozen backend
 * output, so the numbers on them are the ones that were reviewed. The demo page
 * calls the backend live. The fixture picker therefore belongs to the first two
 * and is hidden on the third, where there is no fixture to pick and showing one
 * would suggest the demo were reading it.
 */

import { useState } from "react";

import { FixturePicker } from "@/components/FixturePicker";
import { SyntheticBanner } from "@/components/SyntheticBanner";
import { DEFAULT_FIXTURE_ID, fixtureById } from "@/fixtures";
import { DemoPage } from "@/pages/DemoPage";
import { EvaluationPage, EvaluationRefusal } from "@/pages/EvaluationPage";
import { LandingPage } from "@/pages/LandingPage";
import { LedgerPage, LedgerRefusal } from "@/pages/LedgerPage";
import { isAvailable } from "@/types/report";

const PAGES = [
  {
    id: "landing",
    ordinal: "Overview",
    title: "Overview",
    blurb:
      "A statement of account: what RevTrace recovered, what it can prove it caused, and what it refuses to claim.",
  },
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
  {
    id: "demo",
    ordinal: "Page 3",
    title: "Live demo",
    blurb:
      "One synthetic recovery, end to end — failure, link, signed webhooks, verification, replay, and two refused attacks.",
  },
] as const;

type PageId = (typeof PAGES)[number]["id"];

export default function App() {
  // The landing page is the default view: it is the public entry point, and it
  // is the only page that renders with no request at all.
  const [pageId, setPageId] = useState<PageId>("landing");
  const [fixtureId, setFixtureId] = useState(DEFAULT_FIXTURE_ID);

  const fixture = fixtureById(fixtureId);
  const payload = fixture.payload;
  const page = PAGES.find((p) => p.id === pageId) ?? PAGES[0];
  const isDemo = pageId === "demo";
  const isLanding = pageId === "landing";
  // The fixture picker belongs to the two fixture-driven pages. The landing
  // page reads a fixed committed artifact and the demo page reads none, so
  // offering a picker on either would suggest it changed what they show.
  const showFixturePicker = !isDemo && !isLanding;

  return (
    <div className="min-h-dvh bg-surface">
      <div className="mx-auto max-w-6xl px-4 py-8 sm:px-6 sm:py-10">
        <header
          className={isLanding ? "mb-6" : "mb-6 border-b border-line pb-6"}
        >
          {/* The landing page carries its own statement masthead, so the shell
              contributes only navigation there — two titles would read as two
              documents. */}
          {isLanding ? null : (
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
              {isDemo ? null : <SyntheticBanner label={payload.label} />}
            </div>
          )}

          <nav className={isLanding ? "flex gap-1" : "mt-5 flex gap-1"} aria-label="Pages">
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

        {showFixturePicker ? (
          <div className="mb-8">
            <FixturePicker
              value={fixtureId}
              onChange={setFixtureId}
              description={fixture.description}
            />
          </div>
        ) : null}

        {isLanding ? (
          <LandingPage onOpenDemo={() => setPageId("demo")} />
        ) : isDemo ? (
          <DemoPage />
        ) : isAvailable(payload) ? (
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
          browser formats them and computes nothing.{" "}
          {isDemo
            ? "This page calls the backend live; every figure on it is synthetic and the run is rolled back."
            : "Reading a committed fixture — this page makes no request."}
        </footer>
      </div>
    </div>
  );
}
