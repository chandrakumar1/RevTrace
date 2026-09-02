/**
 * 02 — The experiment.
 *
 * The whole proof in one movement: hold a population back, and the difference
 * between the arms is the answer. The split scene, the money, and the effect
 * live together because they are one argument — separating them across three
 * sections made a reader re-derive the connection three times.
 *
 * The planted truth stays visible as a marker on the interval rather than as its
 * own block. It is the honest caveat on a synthetic result and it belongs
 * *beside* the estimate, but it does not need a paragraph.
 */

import { SplitField, UNITS_PER_DOT } from "@/components/scene/SplitField";
import { Figure, Reveal, Section, SourceLine } from "@/components/ui/Primitives";
import { effect, experiment, ledger } from "@/data/evidence";
import { formatBps, formatBpsInterval, formatCount, formatMinor } from "@/lib/format";
import { usePrefersReducedMotion, useScrollProgress } from "@/lib/motion";
import styles from "./Sections.module.css";

/** Bar widths come straight from the values, so the picture cannot flatter. */
function share(part: number, whole: number): string {
  return `${((part / whole) * 100).toFixed(3)}%`;
}

/** Axis padded either side of the interval. */
const AXIS_LOW = 1200;
const AXIS_HIGH = 1900;
const pos = (bps: number) => ((bps - AXIS_LOW) / (AXIS_HIGH - AXIS_LOW)) * 100;

export function Proof() {
  const reduced = usePrefersReducedMotion();
  const { ref, progress } = useScrollProgress<HTMLDivElement>(!reduced);

  return (
    <Section index="02" eyebrow="The experiment" id="experiment">
      <Reveal>
        <h2 className="h2" id="experiment-heading">
          To measure a cause,
          <br />
          hold something <em className={styles.em}>back.</em>
        </h2>
      </Reveal>

      <Reveal delay={80}>
        <p className={`lede ${styles.sectionLede}`}>
          Every unit is assigned to treatment or holdout by a hash of its identity. The
          holdout is never contacted, so what happens to it is what would have happened
          anyway — and the gap between the arms is the only honest measure of cause.
        </p>
      </Reveal>

      <div className={styles.splitLayout} ref={ref}>
        <SplitField
          progress={progress}
          treatment={experiment.treatment}
          holdout={experiment.holdout}
        />
        <p className={styles.caption}>
          One dot per {formatCount(UNITS_PER_DOT)} units.{" "}
          {formatCount(experiment.totalUnits)} units ·{" "}
          {formatCount(experiment.treatment)} treated ·{" "}
          {formatCount(experiment.holdout)} held back.
        </p>
      </div>

      {/* --- What came back, and what we can claim --------------------- */}

      <Reveal delay={60}>
        <h3 className={`h3 ${styles.subhead}`}>What came back — and what we caused</h3>
      </Reveal>

      <Reveal delay={110}>
        <div
          className={styles.bar}
          role="img"
          aria-label={`Of ${formatMinor(
            ledger.grossRecoveredMinor,
          )} gross recovered, ${formatMinor(
            ledger.incrementalRecoveredMinor,
          )} was incremental and ${formatMinor(
            ledger.creditedNotEarnedMinor,
          )} would have arrived anyway.`}
        >
          <div
            className={styles.barCaused}
            style={{
              width: share(ledger.incrementalRecoveredMinor, ledger.grossRecoveredMinor),
            }}
          />
          <div
            className={styles.barUnearned}
            style={{
              width: share(ledger.creditedNotEarnedMinor, ledger.grossRecoveredMinor),
            }}
          />
        </div>
      </Reveal>

      <div className={styles.ledgerGrid}>
        <Reveal delay={60}>
          <Figure
            label="Gross recovered"
            value={formatMinor(ledger.grossRecoveredMinor)}
            note="What a recovery dashboard reports. Not a claim about cause."
            tone="gross"
            size="md"
          />
        </Reveal>
        <Reveal delay={120}>
          <Figure
            label="Credited, not earned"
            value={formatMinor(ledger.creditedNotEarnedMinor)}
            note={`${formatBps(
              ledger.creditedShareBps,
            )} of gross — money that would have arrived anyway.`}
            tone="unearned"
            size="md"
          />
        </Reveal>
        <Reveal delay={180}>
          <Figure
            label="Incremental recovered"
            value={formatMinor(ledger.incrementalRecoveredMinor)}
            note="The only part the system can claim to have caused."
            tone="caused"
            size="lg"
          />
        </Reveal>
      </div>

      {/* --- The effect, with its interval ----------------------------- */}

      <Reveal delay={60}>
        <h3 className={`h3 ${styles.subhead}`}>The effect, with its uncertainty</h3>
      </Reveal>

      <Reveal delay={110}>
        <div className={styles.interval}>
          <div className={styles.intervalTrack}>
            <div
              className={styles.intervalBand}
              style={{
                left: `${pos(effect.ciLowBps)}%`,
                width: `${pos(effect.ciHighBps) - pos(effect.ciLowBps)}%`,
              }}
            />
            <div className={styles.intervalPoint} style={{ left: `${pos(effect.ateBps)}%` }}>
              <span className={styles.intervalPointLabel}>{formatBps(effect.ateBps)}</span>
            </div>
            <div
              className={styles.intervalTruth}
              style={{ left: `${pos(effect.trueAteBps)}%` }}
            >
              <span className={styles.intervalTruthLabel}>
                planted {formatBps(effect.trueAteBps)}
              </span>
            </div>
          </div>
          <div className={styles.intervalEnds}>
            <span className="mono">{formatBps(effect.ciLowBps)}</span>
            <span className="mono">{formatBps(effect.ciHighBps)}</span>
          </div>
        </div>
      </Reveal>

      <Reveal delay={60}>
        <p className={styles.proofNote}>
          Recovery rate rose {formatBps(effect.ateBps)}, interval{" "}
          {formatBpsInterval(effect.ciLowBps, effect.ciHighBps)}. The estimate is reported
          with its range, never alone. The <em>planted</em> marker is the effect written into
          the generator — knowable only because this population is synthetic, and kept
          separate from the estimate for exactly that reason.
        </p>
      </Reveal>

      <Reveal>
        <SourceLine
          source={ledger.source}
          extra="Money is integer minor units; effects are integer basis points."
        />
      </Reveal>
    </Section>
  );
}
