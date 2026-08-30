/**
 * Page 1 — the Incrementality Ledger.
 *
 * The whole page makes one argument: gross recovery is not a claim about cause,
 * and the difference between the two is measurable. Credited-not-earned is
 * given the same visual weight as gross precisely because it is the number a
 * conventional recovery dashboard silently keeps.
 *
 * Every figure is a fixture value passed through a formatter. Nothing here
 * subtracts, divides, or estimates — the backend did that work in integer
 * arithmetic, and recomputing any of it in the browser would be a second,
 * unverified implementation of the thing the project is trying to prove.
 */

import { DefinitionRow, Panel } from "@/components/Panel";
import { StatCard } from "@/components/StatCard";
import { Waterfall } from "@/components/Waterfall";
import {
  formatBps,
  formatBpsInterval,
  formatCount,
  formatMinor,
  formatMinorInterval,
  formatTimestamp,
} from "@/lib/format";
import type { AvailableReport, UnavailableReport } from "@/types/report";

export function LedgerPage({ report }: { report: AvailableReport }) {
  const { ledger, recovery, experiment, power, bootstrap } = report;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Gross recovered"
          value={formatMinor(ledger.gross_recovered)}
          tone="gross"
          note="Everything the treated arm recovered. What a recovery dashboard reports — and not a claim about cause."
        />
        <StatCard
          label="Credited-not-earned"
          value={formatMinor(ledger.credited_not_earned)}
          tone="credited"
          note="The part of gross that would have arrived with no intervention at all. Measured against a randomised holdout, not assumed."
        />
        <StatCard
          label="Incremental recovered"
          value={formatMinor(ledger.incremental_recovered)}
          tone="incremental"
          emphasis
          detail={
            <>95% CI {formatMinorInterval(ledger.incremental_ci_low, ledger.incremental_ci_high)}</>
          }
          note="The only figure the system can claim to have caused."
        />
        <StatCard
          label="Share never caused"
          value={formatBps(ledger.credited_share_bps)}
          tone="credited"
          note="Credited-not-earned as a proportion of gross. The overstatement a naive attribution would carry."
        />
      </div>

      <Panel
        title="Where the money goes"
        subtitle="Start at what was recovered, remove what would have arrived anyway, and what remains is the effect. Bar lengths are proportional to gross."
      >
        <Waterfall
          gross={ledger.gross_recovered}
          creditedNotEarned={ledger.credited_not_earned}
          incremental={ledger.incremental_recovered}
          incrementalCiLow={ledger.incremental_ci_low}
          incrementalCiHigh={ledger.incremental_ci_high}
        />
      </Panel>

      <div className="grid gap-6 lg:grid-cols-2">
        <Panel
          title="Recovery rate (ITT)"
          subtitle="Intention-to-treat: units stay in their assigned arm even where execution failed, so the denominator is fixed at randomisation."
        >
          <dl>
            <DefinitionRow term="Treated">
              {formatBps(recovery.rate_treatment_bps)}
              <span className="ml-2 font-normal text-muted">
                {formatCount(recovery.n_treatment)} units
              </span>
            </DefinitionRow>
            <DefinitionRow term="Holdout">
              {formatBps(recovery.rate_holdout_bps)}
              <span className="ml-2 font-normal text-muted">
                {formatCount(recovery.n_holdout)} units
              </span>
            </DefinitionRow>
            <DefinitionRow term="Average treatment effect">
              {formatBps(recovery.ate_bps)}
            </DefinitionRow>
            <DefinitionRow term="95% CI">
              {formatBpsInterval(recovery.ate_ci_low_bps, recovery.ate_ci_high_bps)}
            </DefinitionRow>
            <DefinitionRow term="Mean per unit, treated">
              {formatMinor(ledger.mean_treatment)}
            </DefinitionRow>
            <DefinitionRow term="Mean per unit, holdout">
              {formatMinor(ledger.mean_holdout)}
            </DefinitionRow>
          </dl>
        </Panel>

        <Panel
          title="How this was measured"
          subtitle="Pre-registered before the data existed. The design is fixed; the result is whatever it is."
        >
          <dl>
            <DefinitionRow term="Experiment">{experiment.name}</DefinitionRow>
            <DefinitionRow term="Holdout share">
              {formatBps(experiment.holdout_bps)}
            </DefinitionRow>
            <DefinitionRow term="Units per arm">
              {formatCount(power.achieved_n_per_arm)} of {formatCount(power.planned_n_per_arm)}{" "}
              planned
            </DefinitionRow>
            <DefinitionRow term="Underpowered">
              {power.is_underpowered ? (
                <span className="font-semibold text-synthetic">yes</span>
              ) : (
                "no"
              )}
            </DefinitionRow>
            <DefinitionRow term="Bootstrap">
              {formatCount(bootstrap.resamples)} resamples, seed {formatCount(bootstrap.seed)}
            </DefinitionRow>
            <DefinitionRow term="Locked at">
              {formatTimestamp(experiment.locked_at)}
            </DefinitionRow>
          </dl>
        </Panel>
      </div>

      {power.is_underpowered ? (
        <p className="rounded-md border border-synthetic/40 bg-synthetic/10 px-4 py-3 text-sm text-ink">
          <strong className="font-semibold">Interim reading — underpowered.</strong> Achieved{" "}
          {formatCount(power.achieved_n_per_arm)} units per arm against a pre-registered plan of{" "}
          {formatCount(power.planned_n_per_arm)}. The interval is wider than the design intended.
        </p>
      ) : null}
    </div>
  );
}

/**
 * What the page shows when there is no report.
 *
 * The backend refuses to estimate from a population whose observation windows
 * are still open — analysing a partial population would measure the units that
 * resolved quickly rather than a random sample. A zeroed-out ledger would be a
 * fabrication, so the refusal is shown instead.
 */
export function LedgerRefusal({ report }: { report: UnavailableReport }) {
  return (
    <Panel
      title="No ledger available"
      subtitle="The analysis was refused. Nothing is estimated from a partial population."
    >
      {/* The backend's message embeds example risk ids — 37-character unbroken
          tokens that would push the layout sideways on a narrow screen without
          an explicit break. */}
      <p className="mb-4 max-w-prose break-words text-sm leading-relaxed text-ink">
        {report.refusal.message}
      </p>
      <dl>
        <DefinitionRow term="Experiment">{report.experiment.name}</DefinitionRow>
        <DefinitionRow term="Enrolled">{formatCount(report.enrolled)}</DefinitionRow>
        <DefinitionRow term="Sealed outcomes">
          {formatCount(report.sealed_outcomes)}
        </DefinitionRow>
        <DefinitionRow term="Still open">
          {formatCount(report.refusal.unsealed_outcomes)}
        </DefinitionRow>
        <DefinitionRow term="Missing outcomes">
          {formatCount(report.refusal.missing_outcomes)}
        </DefinitionRow>
      </dl>
    </Panel>
  );
}
