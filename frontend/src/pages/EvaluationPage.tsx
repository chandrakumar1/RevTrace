/**
 * Page 2 — Evaluation.
 *
 * Everything the uplift model measured, plus what it does not establish. The
 * two carry equal weight on purpose: several of the strongest-looking numbers
 * here are qualified by a limitation, and a reader who sees one without the
 * other has been misled.
 *
 * Every figure is a payload value through a formatter. Nothing recomputes an
 * ATE, an interval, a Qini coefficient, a quadrant, a harm effect or a net
 * value — the backend did that in integer arithmetic, and a second
 * implementation in the browser would be unverified by construction.
 */

import { AcceptanceResult } from "@/components/AcceptanceResult";
import { CaptureComparison } from "@/components/CaptureComparison";
import { ConfusionMatrix } from "@/components/ConfusionMatrix";
import { CountBars } from "@/components/CountBars";
import type { CountBarRow } from "@/components/CountBars";
import { LimitationsList } from "@/components/LimitationsList";
import { DefinitionRow, Panel } from "@/components/Panel";
import { QuadrantDistribution } from "@/components/QuadrantDistribution";
import { StatCard } from "@/components/StatCard";
import {
  formatBps,
  formatBpsInterval,
  formatCount,
  formatOptionalBps,
  formatPValueMicros,
  formatTimestamp,
} from "@/lib/format";
import type { AvailableReport, UnavailableReport } from "@/types/report";

function toRows(counts: Record<string, number>, color: string): CountBarRow[] {
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([key, count]) => ({ key, label: key, count, color }));
}

function NoUpliftModel() {
  return (
    <Panel title="Uplift model" subtitle="Not fitted for this run.">
      <p className="text-sm text-muted">
        This report carries no uplift section, so there is no Qini curve, no quadrant
        assignment and no confusion matrix to show. Nothing is estimated in their place.
      </p>
    </Panel>
  );
}

export function EvaluationPage({ report }: { report: AvailableReport }) {
  const { experiment, power, recovery, harm, balance, accuracy, bootstrap, uplift } = report;

  return (
    <div className="space-y-6">
      {/* 1. Experiment / run metadata */}
      <Panel
        title="1 · Experiment and run"
        subtitle="Pre-registered before the data existed. The design is fixed; the result is whatever it is."
      >
        <div className="grid gap-x-8 lg:grid-cols-2">
          <dl>
            <DefinitionRow term="Experiment">{experiment.name}</DefinitionRow>
            <DefinitionRow term="Primary metric">
              <code className="text-xs">{experiment.primary_metric}</code>
            </DefinitionRow>
            <DefinitionRow term="Holdout share">{formatBps(experiment.holdout_bps)}</DefinitionRow>
            <DefinitionRow term="alpha / power">
              {formatBps(experiment.alpha_bps)} / {formatBps(experiment.power_bps)}
            </DefinitionRow>
            <DefinitionRow term="Pre-registered MDE">{formatBps(experiment.mde_bps)}</DefinitionRow>
          </dl>
          <dl>
            <DefinitionRow term="Locked at">{formatTimestamp(experiment.locked_at)}</DefinitionRow>
            <DefinitionRow term="Started at">{formatTimestamp(experiment.started_at)}</DefinitionRow>
            <DefinitionRow term="Bootstrap">
              {formatCount(bootstrap.resamples)} resamples, seed {formatCount(bootstrap.seed)}
            </DefinitionRow>
            {uplift ? (
              <>
                <DefinitionRow term="Model">
                  <code className="text-xs">{uplift.model.version}</code>
                </DefinitionRow>
                <DefinitionRow term="Cross-fitting">
                  {uplift.model.folds} folds, {formatCount(uplift.model.n_scored)} units scored
                </DefinitionRow>
              </>
            ) : null}
          </dl>
        </div>
      </Panel>

      {/* 2. Recovery ATE + CI, with 3. Qini alongside */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Recovery ATE (ITT)"
          value={formatBps(recovery.ate_bps)}
          tone="incremental"
          emphasis
          detail={<>95% CI {formatBpsInterval(recovery.ate_ci_low_bps, recovery.ate_ci_high_bps)}</>}
          note="Intention-to-treat: units stay in their assigned arm even where execution failed."
        />
        <StatCard
          label="Against known effect"
          value={formatBps(accuracy.error_bps)}
          tone="plain"
          detail={<>true {formatBps(accuracy.true_ate_bps)}</>}
          note={
            accuracy.interval_covers_the_truth
              ? "The interval contains the planted value. Only checkable because the data is synthetic."
              : "The interval does NOT contain the planted value."
          }
        />
        <StatCard
          label="Qini coefficient"
          value={uplift ? formatOptionalBps(uplift.qini.qini_coefficient_bps) : "—"}
          tone={uplift?.qini.beats_random ? "incremental" : "plain"}
          detail={
            uplift ? (
              <>Q(N) = {formatCount(uplift.qini.qini_total)} incremental recoveries</>
            ) : null
          }
          note={
            !uplift
              ? "No uplift model was fitted for this run."
              : uplift.qini.qini_coefficient_bps === null
                ? "Undefined — Q(N) is zero, so there is no incremental recovery to apportion. Undefined is not the same as zero."
                : uplift.qini.beats_random
                  ? "Positive: the ranking captured more than a random order would."
                  : "Negative: the ranking did worse than chance. Reported as measured, not clamped."
          }
        />
        <StatCard
          label="Harm ATE"
          value={formatBps(harm.ate_bps)}
          tone={harm.ate_bps > 0 ? "credited" : "plain"}
          detail={<>95% CI {formatBpsInterval(harm.ate_ci_low_bps, harm.ate_ci_high_bps)}</>}
          note="Mandate cancellation, pre-registered as a first-class outcome rather than a footnote."
        />
      </div>

      {power.is_underpowered ? (
        <p className="rounded-md border border-synthetic/40 bg-synthetic/10 px-4 py-3 text-sm text-ink">
          <strong className="font-semibold">Interim reading — underpowered.</strong> Achieved{" "}
          {formatCount(power.achieved_n_per_arm)} units per arm against a pre-registered plan of{" "}
          {formatCount(power.planned_n_per_arm)}. Every interval below is wider than the design
          intended, and quadrant labels are correspondingly less reliable.
        </p>
      ) : null}

      {uplift === null ? (
        <NoUpliftModel />
      ) : (
        <>
          {/* 4. Top-20% capture */}
          <Panel
            title="4 · Top-share capture"
            subtitle="How much of the incremental effect the highest-ranked units account for — counted two ways."
          >
            <CaptureComparison
              byCount={uplift.top_capture}
              byAmount={uplift.top_amount_capture}
            />
          </Panel>

          {/* 5. Quadrant distribution */}
          <Panel
            title="5 · Quadrant distribution"
            subtitle="Only one of the five means act. All five are listed, including any nothing landed in."
          >
            <QuadrantDistribution
              counts={uplift.quadrant_counts}
              emptyQuadrants={uplift.confusion_matrix.empty_quadrants}
              total={uplift.model.n_scored}
            />
          </Panel>

          {/* 6. Confusion matrix */}
          <Panel
            title="6 · Quadrant against planted stratum"
            subtitle="Only possible because the data is synthetic. The model saw observed features alone; this comparison happens strictly afterwards and never feeds back."
          >
            <ConfusionMatrix matrix={uplift.confusion_matrix} />
          </Panel>

          {/* 7. Fold-local thresholds */}
          <Panel
            title="7 · Fold-local thresholds"
            subtitle="Each fold's boundaries come from the four folds it trained on, and apply only to the fold it held out. Derived from the whole population instead, a unit's own outcome could move the boundary it is judged against."
          >
            <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
              <table className="w-full min-w-[38rem] border-collapse text-sm">
                <thead>
                  <tr className="border-b border-line text-left">
                    <th scope="col" className="py-2 pr-4 font-medium text-muted">Fold</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium text-muted">Self-recovery ceiling</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium text-muted">Low tertile</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium text-muted">High tertile</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium text-muted">Harm threshold</th>
                    <th scope="col" className="py-2 text-right font-medium text-muted">Trained on</th>
                  </tr>
                </thead>
                <tbody>
                  {uplift.fold_thresholds.map((t) => (
                    <tr key={t.fold} className="border-b border-line/60 last:border-b-0">
                      <th scope="row" className="py-2 pr-4 text-left font-normal text-ink">{t.fold}</th>
                      <td className="tnum py-2 pr-4 text-right">{formatBps(t.self_recovery_ceiling_bps)}</td>
                      <td className="tnum py-2 pr-4 text-right">{formatBps(t.low_tertile_bps)}</td>
                      <td className="tnum py-2 pr-4 text-right">{formatBps(t.high_tertile_bps)}</td>
                      <td className="tnum py-2 pr-4 text-right">{formatBps(t.harm_threshold_bps)}</td>
                      <td className="tnum py-2 text-right text-muted">{formatCount(t.training_size)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </>
      )}

      {/* 8. Balance and power. Outside the uplift block on purpose: balance and
          power are properties of the randomisation, and hold whether or not an
          uplift model was fitted. The block is split here so the section still
          renders in its numbered position. */}
      <Panel
        title="8 · Balance and power"
        subtitle="Randomisation is checked rather than assumed. A flagged covariate means the arms differ more than chance comfortably explains."
      >
        <div className="grid gap-6 lg:grid-cols-2">
          <dl>
            <DefinitionRow term="Verdict">
              {balance.is_balanced ? (
                "balanced"
              ) : (
                <span className="font-semibold text-synthetic">imbalance flagged</span>
              )}
            </DefinitionRow>
            <DefinitionRow term="Threshold">|SMD| &gt; {formatBps(balance.threshold_bps)}</DefinitionRow>
            <DefinitionRow term="Arms">
              {formatCount(balance.treatment_n)} treated / {formatCount(balance.holdout_n)} holdout
            </DefinitionRow>
            <DefinitionRow term="Flagged covariates">
              {balance.flagged.length === 0 ? "none" : balance.flagged.join(", ")}
            </DefinitionRow>
          </dl>
          <dl>
            <DefinitionRow term="Achieved per arm">
              {formatCount(power.achieved_n_per_arm)}
            </DefinitionRow>
            <DefinitionRow term="Planned per arm">
              {formatCount(power.planned_n_per_arm)}
            </DefinitionRow>
            <DefinitionRow term="Required per arm">
              {formatCount(power.required_n_per_arm)}
            </DefinitionRow>
            <DefinitionRow term="Detectable effect">
              {formatBps(power.detectable_mde_bps)}
            </DefinitionRow>
            <DefinitionRow term="p-value">
              {formatPValueMicros(recovery.p_value_micros)}
            </DefinitionRow>
          </dl>
        </div>
      </Panel>

      {uplift !== null ? (
        <>
          {/* 9. Gray Zone breakdown */}
          <Panel
            title="9 · Gray Zone, by cause"
            subtitle="Two different situations produce the same label: a cell that never qualified, and a qualified cell whose result matched no rule. Counted apart because they mean different things."
          >
            <p className="mb-4 text-sm text-ink">
              Total Gray Zone:{" "}
              <strong className="tnum font-semibold">{formatCount(uplift.gray_zone.total)}</strong>{" "}
              of {formatCount(uplift.model.n_scored)} scored
            </p>
            <div className="grid gap-6 lg:grid-cols-2">
              <div>
                <h3 className="mb-3 text-[0.7rem] font-semibold uppercase tracking-widest text-muted">
                  By quadrant rule
                </h3>
                <CountBars rows={toRows(uplift.gray_zone.by_rule, "bg-line")} />
              </div>
              <div>
                <h3 className="mb-3 text-[0.7rem] font-semibold uppercase tracking-widest text-muted">
                  By cell qualification reason
                </h3>
                <CountBars rows={toRows(uplift.gray_zone.by_reason, "bg-neutral")} />
              </div>
            </div>
          </Panel>

          {/* 10. Cell ladder / usage */}
          <Panel
            title="10 · Cell ladder and usage"
            subtitle="Which rung each unit was scored at. A global fallback means neither rung qualified, so the estimate is an unconditional average rather than a conditional one."
          >
            <div className="grid gap-6 lg:grid-cols-2">
              <div>
                <h3 className="mb-3 text-[0.7rem] font-semibold uppercase tracking-widest text-muted">
                  By ladder level
                </h3>
                <CountBars rows={toRows(uplift.ladder.by_level, "bg-gross")} />
                <dl className="mt-4">
                  <DefinitionRow term="Distinct cells">
                    {formatCount(uplift.ladder.distinct_cells)}
                  </DefinitionRow>
                  <DefinitionRow term="Global fallbacks">
                    <span className={uplift.ladder.global_fallbacks > 0 ? "text-synthetic" : ""}>
                      {formatCount(uplift.ladder.global_fallbacks)}
                    </span>
                  </DefinitionRow>
                </dl>
              </div>
              <div>
                <h3 className="mb-3 text-[0.7rem] font-semibold uppercase tracking-widest text-muted">
                  Units per cell
                </h3>
                <CountBars
                  rows={uplift.ladder.cells.map((c) => ({
                    key: c.cell,
                    label: c.cell,
                    count: c.n,
                    color: "bg-gross",
                  }))}
                />
              </div>
            </div>
          </Panel>

          {/* 11. Harm summary */}
          <Panel
            title="11 · Harm uplift"
            subtitle="Computed in memory to decide sleeping-dog labels, then discarded — there is no column for it, by design. This report is the only place it is visible."
          >
            <div className="grid gap-x-8 sm:grid-cols-2">
              <dl>
                <DefinitionRow term="Units">{formatCount(uplift.harm_uplift.n)}</DefinitionRow>
                <DefinitionRow term="Minimum">{formatBps(uplift.harm_uplift.min_bps)}</DefinitionRow>
                <DefinitionRow term="Mean">{formatBps(uplift.harm_uplift.mean_bps)}</DefinitionRow>
              </dl>
              <dl>
                <DefinitionRow term="Maximum">{formatBps(uplift.harm_uplift.max_bps)}</DefinitionRow>
                <DefinitionRow term="Positive harm uplift">
                  {formatCount(uplift.harm_uplift.positive)}
                </DefinitionRow>
                <DefinitionRow term="Above fold threshold">
                  {formatCount(uplift.harm_uplift.above_fold_threshold)}
                </DefinitionRow>
              </dl>
            </div>
          </Panel>

          {/* 12. Acceptance */}
          <Panel
            title="12 · Acceptance criterion"
            subtitle="Reported, not enforced. The report is produced whether the clauses hold or not; a failing clause is a result, not an error."
          >
            <AcceptanceResult acceptance={uplift.acceptance} />
          </Panel>
        </>
      ) : null}

      {/* 13. Limitations */}
      <Panel
        title="13 · Limitations"
        subtitle="What these numbers do not establish. Stated in the report itself, because a caveat kept in documentation is a caveat nobody reads."
      >
        <div className="space-y-6">
          {uplift ? (
            <LimitationsList heading="Of the uplift model" items={uplift.limitations} />
          ) : null}
          <LimitationsList heading="Of the evaluation" items={report.limitations} />
          {report.deferred.length > 0 ? (
            <div>
              <h3 className="mb-2 text-[0.7rem] font-semibold uppercase tracking-widest text-muted">
                Deferred sections
              </h3>
              <ul className="space-y-2">
                {report.deferred.map((entry) => (
                  <li key={entry.section} className="text-xs leading-relaxed break-words">
                    <strong className="font-semibold text-ink">{entry.section}</strong>
                    <span className="text-faint"> — {entry.reason}.</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </Panel>
    </div>
  );
}

/** The refusal state — no report exists, so nothing is estimated in its place. */
export function EvaluationRefusal({ report }: { report: UnavailableReport }) {
  return (
    <Panel
      title="No evaluation available"
      subtitle="The analysis was refused. Nothing is estimated from a partial population."
    >
      <p className="mb-4 max-w-prose break-words text-sm leading-relaxed text-ink">
        {report.refusal.message}
      </p>
      <dl>
        <DefinitionRow term="Experiment">{report.experiment.name}</DefinitionRow>
        <DefinitionRow term="Enrolled">{formatCount(report.enrolled)}</DefinitionRow>
        <DefinitionRow term="Sealed outcomes">{formatCount(report.sealed_outcomes)}</DefinitionRow>
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
