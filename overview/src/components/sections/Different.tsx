/**
 * 03 — Why RevTrace is different.
 *
 * Three differentiators, one screen. Each is a claim a competing recovery tool
 * cannot make, stated once and evidenced once:
 *
 *   it declines to act · the AI cannot touch the numbers · it refuses bad events
 *
 * Deliberately compressed from three former sections. The long-form arguments
 * belonged in a technical write-up; a judge needs the claim and the proof of it.
 */

import { Reveal, Section } from "@/components/ui/Primitives";
import { aiCapture, crossTenantDefect, gate } from "@/data/evidence";
import { formatCount } from "@/lib/format";
import styles from "./Sections.module.css";

const PIPELINE = ["Evidence", "Deterministic core", "Causal result", "AI interpretation"];

export function Different() {
  return (
    <Section index="03" eyebrow="Why RevTrace is different" id="different">
      <Reveal>
        <h2 className="h2" id="different-heading">
          Three things a recovery tool
          <br />
          usually <em className={styles.em}>cannot say.</em>
        </h2>
      </Reveal>

      <div className={styles.diffs}>
        {/* --- 1. Abstention ------------------------------------------ */}
        <Reveal delay={60}>
          <article className={styles.diff}>
            <p className="meta">01 — It declines to act</p>
            <h3 className={`h3 ${styles.diffHead}`}>
              The best intervention is often none.
            </h3>
            <p className={styles.diffBody}>
              Contacting someone who would have paid anyway costs money and goodwill. Every
              decision passes a gate that can refuse, and each refusal is recorded with a
              named reason.
            </p>
            <div className={styles.gateStrip}>
              <div>
                <p className="meta">Without the gate</p>
                <p className={styles.gateValue}>{formatCount(gate.gateOffActed)}</p>
              </div>
              <span className={styles.gateArrow} aria-hidden>
                →
              </span>
              <div>
                <p className="meta">With the gate</p>
                <p className={styles.gateValue} data-tone="caused">
                  {formatCount(gate.gateOnActed)}
                </p>
              </div>
              <div>
                <p className="meta">Deliberate abstentions</p>
                <p className={styles.gateValue} data-tone="muted">
                  {formatCount(gate.gateOnAbstained)}
                </p>
              </div>
            </div>
          </article>
        </Reveal>

        {/* --- 2. AI boundary ----------------------------------------- */}
        <Reveal delay={120}>
          <article className={styles.diff}>
            <p className="meta">02 — The AI holds no authority</p>
            <h3 className={`h3 ${styles.diffHead}`}>
              AI explains the evidence. It does not rewrite it.
            </h3>
            <p className={styles.diffBody}>
              Revenue, risk, uplift, policy and execution are ordinary tested integer
              arithmetic. The model proposes falsifiable hypotheses and cites the integers it
              relies on — deterministic code then checks them before anything is believed.
            </p>
            <ol className={styles.flow}>
              {PIPELINE.map((stage, i) => (
                <li key={stage} className={styles.flowStage} data-core={i === 1}>
                  {stage}
                </li>
              ))}
            </ol>
            <p className={styles.diffFoot}>
              One live capture on <span className="mono">{aiCapture.cellKey}</span> returned{" "}
              <span className="mono">{aiCapture.verdict}</span> against a deterministic check.
              That run was rolled back, so no persisted audit record exists.
            </p>
          </article>
        </Reveal>

        {/* --- 3. Trust boundary -------------------------------------- */}
        <Reveal delay={180}>
          <article className={styles.diff}>
            <p className="meta">03 — It refuses bad events</p>
            <h3 className={`h3 ${styles.diffHead}`}>
              A payment event is a stranger's bytes until proven otherwise.
            </h3>
            <p className={styles.diffBody}>
              Signatures are verified on the raw bytes before anything parses them, ownership
              is derived from the signed payload rather than from what a caller claims, and a
              replayed delivery writes nothing.
            </p>
            <ul className={styles.refusals}>
              <li className={styles.refusal}>
                <span aria-hidden>✓</span> Tampered body — refused
              </li>
              <li className={styles.refusal}>
                <span aria-hidden>✓</span> Foreign merchant — refused
              </li>
            </ul>
            <p className={styles.diffFoot}>{crossTenantDefect.headline} Ownership now comes
              from the signed payment id, and the attack is a regression test.</p>
          </article>
        </Reveal>
      </div>
    </Section>
  );
}
