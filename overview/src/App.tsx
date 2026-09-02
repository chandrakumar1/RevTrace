/**
 * The RevTrace overview — five movements.
 *
 *   Hook · Problem · Experiment · Why it is different · See it
 *
 * A judge-facing showcase, not a technical write-up: each section makes one
 * point and evidences it once. A public storytelling layer, entirely
 * independent of the RevTrace application — no API, no backend dependency, no
 * shared components. Every figure is compiled in from `data/evidence.ts`, so the
 * page renders identically whether or not the application is running.
 */

import { Different } from "@/components/sections/Different";
import { Hero } from "@/components/sections/Hero";
import { Outro } from "@/components/sections/Outro";
import { Problem } from "@/components/sections/Problem";
import { Proof } from "@/components/sections/Proof";

export default function App() {
  return (
    <>
      {/* Skip link: the page is long and scene-heavy, so a keyboard user gets a
          direct route to the only two things they may have come for. */}
      <a className="skip" href="#open">
        Skip to project links
      </a>

      <div className="field" aria-hidden />

      <div className="shell">
        <Hero />
        <main>
          <Problem />
          <Proof />
          <Different />
          <Outro />
        </main>
      </div>
    </>
  );
}
