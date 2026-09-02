/**
 * The public landing page — a statement of account, not a product page.
 *
 * Eight numbered sections, in the order the argument has to be made: the claim,
 * the ledger that supports it, what is and is not proven, what the system
 * refuses to do, where the AI's authority ends, a defect that was found and
 * fixed, the recovery path itself, and everything this does not establish.
 *
 * **Renders entirely from committed artifacts.** Not one request is made to
 * load this page — it is correct with the backend asleep, unreachable, or not
 * yet deployed. The only interaction that touches the network is an explicit
 * click through to the live demo.
 *
 * **No statistic is typed into this file.** Every figure comes from
 * `@/data/landing`, which reads the committed artifacts and computes nothing.
 * A number written here by hand would be a number nobody could trace. The only
 * literals below are section headings and prose.
 *
 * **`null` is rendered as undefined, never as zero.** An absent quantity means
 * the measurement does not exist; showing `0` would turn that into a
 * measurement.
 */

import { LimitationsList } from "@/components/LimitationsList";
import {
  Comparison,
  LedgerRow,
  Prose,
  Provenance,
  Section,
  StatementHeader,
  Subtotal,
} from "@/components/landing/Statement";
import {
  abstentionReasons,
  accuracy,
  deferred,
  demo,
  effect,
  experimentDesign,
  gate,
  grayZone,
  hypothesisCapture,
  ledger,
  limitations,
  provenance,
  uplift,
} from "@/data/landing";
import {
  formatBps,
  formatBpsInterval,
  formatCount,
  formatMinor,
  formatMinorInterval,
  formatPValueMicros,
  formatTimestamp,
  UNDEFINED,
} from "@/lib/format";

/** A tone for a captured demo step. `refused` marks a control that worked. */
const STEP_ACCENT: Record<string, string> = {
  plain: "border-line",
  verified: "border-incremental/40",
  refused: "border-incremental/40",
};

export function LandingPage({ onOpenDemo }: { onOpenDemo: () => void }) {
  return (
    <div className="space-y-10 pb-4">
      <StatementHeader
        title="RevTrace"
        subtitle={
          "A revenue recovery system that can prove how much of the money it recovered it " +
          "actually caused — and that refuses to act when it cannot. This page is a " +
          "statement of account: every figure below is traceable to a committed artifact, " +
          "and every figure below is synthetic."
        }
        meta={[
          { label: "Statement", value: provenance.evaluationLabel },
          { label: "Experiment", value: provenance.experimentName },
          // `locked_at` is nullable in the contract; the formatter renders an
          // absent timestamp as a dash rather than an empty cell.
          { label: "Locked at", value: formatTimestamp(provenance.lockedAt) },
          { label: "Seed", value: formatCount(provenance.seed) },
        ]}
      />

      {/* 1 ---------------------------------------------------------------- */}
      <Section
        ordinal="01"
        title="What this is, and what it is not"
        lede={
          "Most recovery tools report the money that arrived after they acted. Many of those " +
          "customers would have paid anyway. RevTrace measures the difference against a " +
          "randomised holdout, and reports the part it cannot claim."
        }
      >
        <div className="rounded-md border border-synthetic/40 bg-synthetic/10 p-4">
          <p className="text-xs font-semibold uppercase tracking-widest text-synthetic">
            {provenance.demoProvenance}
          </p>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted">
            No real Razorpay transaction has ever been processed by this system. No real
            customer, no real payment, no real money. Every measurement on this page comes
            from a generated population with planted effects — recovering a planted effect
            validates the estimator, not the world.
          </p>
        </div>
        <Provenance>
          Pre-registered hypothesis, frozen before the run: “{provenance.hypothesis}”
        </Provenance>
      </Section>

      {/* 2 ---------------------------------------------------------------- */}
      <Section
        ordinal="02"
        title="The incrementality ledger"
        lede={
          "Three quantities describing the same money. The gap between the first two is the " +
          "number a conventional dashboard would have taken credit for."
        }
      >
        <LedgerRow
          label="Gross recovered"
          value={formatMinor(ledger.grossRecovered)}
          note="Everything that arrived in the treatment arm. Not a claim about cause."
          tone="gross"
        />
        <LedgerRow
          label="Credited, not earned"
          value={formatMinor(ledger.creditedNotEarned)}
          note={
            `${formatBps(ledger.creditedShareBps)} of gross. Money that arrived without ` +
            `this system causing it — visible only because a holdout was kept.`
          }
          tone="credited"
        />
        <Subtotal
          label="Incremental recovered"
          value={formatMinor(ledger.incrementalRecovered)}
          note={
            `95% interval ${formatMinorInterval(ledger.incrementalCiLow, ledger.incrementalCiHigh)}. ` +
            `The only part this system can claim to have caused.`
          }
        />
        <Provenance>
          Intention-to-treat over {formatCount(ledger.treatmentCount)} treated and{" "}
          {formatCount(ledger.holdoutCount)} held-out cases; percentile bootstrap,{" "}
          {formatCount(provenance.resamples)} resamples, seed {formatCount(provenance.bootstrapSeed)}.
          A case stays in the arm it was assigned to even when execution failed.
        </Provenance>
      </Section>

      {/* 3 ---------------------------------------------------------------- */}
      <Section
        ordinal="03"
        title="Proven, and unproven"
        lede={
          "The primary effect, its interval, and — kept beside it — the sections the backend " +
          "declined to report at all."
        }
      >
        <LedgerRow
          label="Effect on recovery rate"
          value={formatBps(effect.ateBps)}
          note={`95% interval ${formatBpsInterval(effect.ciLowBps, effect.ciHighBps)}, p ${formatPValueMicros(effect.pValueMicros)}.`}
          tone="incremental"
          emphasis
        />
        <LedgerRow
          label="Treatment arm recovery rate"
          value={formatBps(effect.rateTreatmentBps)}
          note={`${formatCount(effect.treatmentCount)} cases`}
        />
        <LedgerRow
          label="Holdout recovery rate"
          value={formatBps(effect.rateHoldoutBps)}
          note={`${formatCount(effect.holdoutCount)} cases, untreated`}
        />
        <LedgerRow
          label="Estimate against the planted truth"
          value={formatBps(accuracy.trueAteBps)}
          note={
            `Estimated ${formatBps(accuracy.estimatedAteBps)}, error ${formatBps(accuracy.errorBps)}. ` +
            `The interval ${accuracy.intervalCoversTheTruth ? "covers" : "does not cover"} the truth — ` +
            `knowable only because the truth was written by hand.`
          }
          tone="muted"
        />
        <LedgerRow
          label="Design"
          value={`${formatBps(experimentDesign.holdoutBps)} holdout`}
          note={
            `${formatCount(experimentDesign.achievedNPerArm)} per arm against a pre-registered ` +
            `${formatCount(experimentDesign.plannedNPerArm)}; underpowered: ` +
            `${String(experimentDesign.isUnderpowered)}; balanced: ` +
            `${String(experimentDesign.isBalanced)} with ` +
            `${formatCount(experimentDesign.flaggedCovariates)} covariates flagged.`
          }
          tone="muted"
        />

        <div className="mt-6 rounded-md border border-line bg-card p-4">
          <h3 className="text-[0.65rem] font-semibold uppercase tracking-widest text-muted">
            Not reported — {formatCount(deferred.length)} sections
          </h3>
          <ul className="mt-3 space-y-2">
            {deferred.map((item) => (
              <li key={item.section} className="text-xs leading-relaxed">
                <span className="font-medium text-ink">{item.section}</span>
                <span className="text-faint"> — {item.reason}</span>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-xs leading-relaxed text-faint">
            A report that silently omits what it could not compute reads as a report that
            computed everything.
          </p>
        </div>
      </Section>

      {/* 4 ---------------------------------------------------------------- */}
      <Section
        ordinal="04"
        title="Abstention"
        lede={
          "Not acting is a first-class outcome. Contacting a customer who would have paid " +
          "anyway costs money and goodwill, so the gate treats it as a real cost rather than " +
          "a free upside."
        }
      >
        <Comparison
          left={{
            heading: "Gate off — act on everything",
            rows: [
              { label: "Actions", value: formatCount(gate.gateOffActed) },
              { label: "Abstentions", value: formatCount(gate.gateOffAbstained) },
              { label: "Spend", value: formatMinor(gate.gateOffCostMinor) },
            ],
            note: "The counterfactual: every treated case receives the intervention.",
          }}
          right={{
            heading: "Gate on — the policy engine",
            rows: [
              { label: "Actions", value: formatCount(gate.gateOnActed) },
              { label: "Abstentions", value: formatCount(gate.gateOnAbstained) },
              { label: "Spend", value: formatMinor(gate.gateOnCostMinor) },
            ],
            note: `Of which ${formatCount(gate.gateOnExplored)} were taken under the exploration budget (${formatBps(gate.explorationBudgetBps)}).`,
          }}
        />

        <div className="mt-5">
          <LedgerRow
            label="Actions avoided"
            value={formatCount(gate.actionsAvoided)}
            note={`${formatBps(gate.actionReductionBps)} fewer interventions than acting on everything.`}
            tone="incremental"
          />
          <LedgerRow
            label="Spend avoided"
            value={formatMinor(gate.costAvoidedMinor)}
            note={`At a unit cost of ${formatMinor(gate.unitCostMinor)} per ${gate.intervention}.`}
            tone="incremental"
          />
          {abstentionReasons.map((row) => (
            <LedgerRow
              key={row.reason}
              label={row.reason.replaceAll("_", " ")}
              value={formatCount(row.count)}
              tone="muted"
            />
          ))}
        </div>

        <div className="mt-6 rounded-md border border-line bg-card p-4">
          <h3 className="text-[0.65rem] font-semibold uppercase tracking-widest text-muted">
            The gray zone — {formatCount(grayZone.total)} cases
          </h3>
          <p className="mt-2 max-w-2xl text-xs leading-relaxed text-faint">
            The hardest decision the gate makes: cases where the evidence does not clearly
            support acting or abstaining. {formatCount(grayZone.selfRecoveryLikely)} were
            declined because the customer was likely to recover unaided, and{" "}
            {formatCount(grayZone.upliftNotSignificant)} because the uplift interval contained
            zero. {formatCount(grayZone.explored)} were acted on under an explicit exploration
            budget, so the gate keeps learning rather than freezing.
          </p>
        </div>

        <Provenance>
          Decisions only, from a separate artifact — an action count is not an effect. Policy
          revision {gate.policyRevision}, gray-zone policy {gate.grayZonePolicy}, migration{" "}
          {provenance.migration}, over {formatCount(gate.caseCount)} cases.
        </Provenance>
      </Section>

      {/* 5 ---------------------------------------------------------------- */}
      <Section
        ordinal="05"
        title="Where the AI's authority ends"
        lede="The LLM is not the authority over money. It has no path to an execution."
      >
        <Prose>
          <p>
            Every revenue figure, risk score, uplift estimate, policy decision and execution
            authorisation on this page was computed by deterministic, tested, integer
            arithmetic. No language model is anywhere near those numbers.
          </p>
          <p>
            What the model does is propose a <em>falsifiable</em> hypothesis about a cell and
            cite the integers it relies on. Deterministic code then checks every cited integer
            against the computed values and returns a verdict. A model that invents a number is
            caught by arithmetic, not by trust.
          </p>
        </Prose>

        <div className="mt-5 rounded-md border border-line bg-card p-4">
          <h3 className="text-[0.65rem] font-semibold uppercase tracking-widest text-muted">
            Live capture
          </h3>
          <dl className="mt-3">
            {[
              { label: "Cell", value: hypothesisCapture.cellKey },
              { label: "Claim", value: hypothesisCapture.claim },
              { label: "Rule", value: hypothesisCapture.rule },
              { label: "Verdict", value: hypothesisCapture.verdict },
              { label: "Model", value: hypothesisCapture.model },
            ].map((row) => (
              <div
                key={row.label}
                className="flex items-baseline justify-between gap-4 border-b border-line py-1.5 last:border-b-0"
              >
                <dt className="text-xs text-muted">{row.label}</dt>
                <dd className="tnum text-right font-mono text-xs break-all text-ink">
                  {row.value}
                </dd>
              </div>
            ))}
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 pt-2">
              <dt className="text-xs text-muted">Integers checked</dt>
              {/* The stored value is null and stays null. Only its presentation
                  changes: a bare "undefined" reads as a rendering fault rather
                  than as the deliberate absence of evidence it is. */}
              <dd className="max-w-prose text-right text-xs text-faint">
                {hypothesisCapture.citedIntegerCount ??
                  "Not reported — capture was rolled back and no audit row exists."}
              </dd>
            </div>
          </dl>
          <p className="mt-3 max-w-2xl text-xs leading-relaxed text-faint">
            The count is not reported because the run was rolled back and never persisted — no
            audit row exists in any database. The classification above survives; the numeric
            detail does not, and a figure recalled rather than read from an artifact is exactly
            what this project refuses everywhere else.
          </p>
        </div>

        {uplift === null ? null : (
          <Provenance>
            Cells are scored by a {formatCount(uplift.folds)}-fold cross-fitted model (
            {uplift.modelVersion}) over {formatCount(uplift.nScored)} units across{" "}
            {formatCount(uplift.distinctCells)} distinct cells, with{" "}
            {formatCount(uplift.globalFallbacks)} falling back to the global rate. Qini
            coefficient{" "}
            {uplift.qiniCoefficientBps === null
              ? UNDEFINED
              : formatBps(uplift.qiniCoefficientBps)}
            .
          </Provenance>
        )}
      </Section>

      {/* 6 ---------------------------------------------------------------- */}
      <Section
        ordinal="06"
        title="A defect we found, and fixed"
        lede="Reported because a system that claims to prove things has to report what it got wrong."
      >
        <Prose>
          <p>
            A webhook arrives signed. Early on, the merchant it belonged to was taken from a
            query parameter — outside the signed bytes. That made it possible to post a
            validly-signed webhook about one merchant's payment while naming another: the event
            was filed under the caller's merchant, pointed at the victim's order, and advanced
            the victim's payment attempt.
          </p>
          <p>
            Checking that the merchant existed caught none of it, because the merchant did
            exist — it simply was not the one that owned the payment. The fix derives the owner
            from the signed payment id, through{" "}
            <span className="font-mono text-xs">payment_attempts → orders → merchant_id</span>.
            The query parameter survives as an <em>assertion</em>: it is checked against the
            derivation and can now only cause a rejection.
          </p>
          <p>
            The attack was re-run against the fixed code — zero rows written, the victim's
            attempt unchanged — and it is now a regression test. You can watch it refuse in the
            live demo below.
          </p>
        </Prose>
        <Provenance>
          The signature is verified over the exact raw bytes before anything parses them, with
          a timing-safe comparison. Idempotency is a database constraint rather than a code
          path, so a duplicate delivery is an insert that writes nothing.
        </Provenance>
      </Section>

      {/* 7 ---------------------------------------------------------------- */}
      <Section
        ordinal="07"
        title="The recovery path"
        lede={
          "One synthetic failed payment, carried end to end. Captured from a real run of the " +
          "same code the live demo executes — shown here as static content so this page needs " +
          "no backend."
        }
      >
        <ol className="space-y-2">
          {demo.steps.map((step) => (
            <li
              key={step.number}
              className={`rounded-md border bg-card p-3 ${STEP_ACCENT[step.tone] ?? "border-line"}`}
            >
              <div className="flex items-baseline gap-3">
                <span className="tnum text-[0.65rem] font-semibold tracking-widest text-faint">
                  {String(step.number).padStart(2, "0")}
                </span>
                <div className="min-w-0">
                  <p className="text-sm font-medium">{step.title}</p>
                  <ul className="mt-1.5 space-y-0.5">
                    {step.facts.map((fact, index) => (
                      <li
                        key={`${fact.label}-${index}`}
                        className="flex flex-wrap items-baseline gap-x-2 text-xs"
                      >
                        <span className="text-faint">{fact.label}</span>
                        <span
                          className={
                            fact.tone === "plain"
                              ? "tnum text-muted"
                              : "tnum font-medium text-incremental"
                          }
                        >
                          {fact.minor === null ? fact.value : formatMinor(fact.minor)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            </li>
          ))}
        </ol>

        <div className="mt-4 flex flex-wrap items-center gap-4 border-t-2 border-ink pt-4">
          <p className="text-sm font-semibold text-incremental">{demo.finalStatus}</p>
          <button
            type="button"
            onClick={onOpenDemo}
            className="rounded-md bg-ink px-4 py-2 text-sm font-semibold text-surface transition-colors hover:opacity-90"
          >
            ▶ Run it live
          </button>
        </div>
        <Provenance>
          The two refusals in step 06 are <strong>successes</strong> — a tampered body and a
          cross-tenant webhook, both rejected by production code. Captured{" "}
          {demo.capturedAt}. {demo.note}
        </Provenance>
      </Section>

      {/* 8 ---------------------------------------------------------------- */}
      <Section
        ordinal="08"
        title="What this does not establish"
        lede="Carried with the same weight as the results, because several of the strongest-looking figures above are qualified by one of these."
      >
        <LimitationsList heading="This run" items={limitations.run} />
        <div className="mt-5">
          <LimitationsList heading="Uplift model" items={limitations.uplift} />
        </div>

        <div className="mt-6 rounded-md border border-line bg-card p-4">
          <h3 className="text-[0.65rem] font-semibold uppercase tracking-widest text-muted">
            For a reviewer — two minutes
          </h3>
          <ol className="mt-3 space-y-2 text-xs leading-relaxed text-muted">
            <li>
              <span className="font-medium text-ink">Section 02.</span> The gap between gross
              and incremental is the pitch. That is the money a conventional dashboard claims.
            </li>
            <li>
              <span className="font-medium text-ink">Section 04.</span> Ask what happens when
              the system is not sure. It abstains, and records which named reason applied.
            </li>
            <li>
              <span className="font-medium text-ink">Section 07 → Run it live.</span> Watch the
              payment advance, the replay change nothing, and both attacks get refused.
            </li>
            <li>
              <span className="font-medium text-ink">Ledger and Evaluation pages.</span> The
              same artifact, in full, including an undefined Qini coefficient rendered as{" "}
              <span className="font-mono">{UNDEFINED}</span> rather than zero.
            </li>
          </ol>
        </div>
      </Section>
    </div>
  );
}
