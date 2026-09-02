/**
 * 04 — See it.
 *
 * Everything narrows to two links. The disclosure is repeated here rather than
 * left behind in the hook: this is the screen a visitor is on when they decide
 * to click through, and the last chance to say what they are about to see.
 */

import { Disclosure, ExternalLink, Reveal } from "@/components/ui/Primitives";
import { DISCLOSURE, links, provenance } from "@/data/evidence";
import styles from "./Sections.module.css";

export function Outro() {
  return (
    <section className={`section ${styles.outro}`} id="open" aria-labelledby="outro-heading">
      <div className="wrap">
        <Reveal>
          <div className="rule">
            <span className="meta">04</span>
            <span className="meta">See it</span>
          </div>
        </Reveal>

        <Reveal delay={60}>
          <h2 className="h2" id="outro-heading">
            The application itself,
            <br />
            <em className={styles.em}>running.</em>
          </h2>
        </Reveal>

        <Reveal delay={120}>
          <p className={`lede ${styles.sectionLede}`}>
            The full ledger and evaluation, plus a synthetic recovery you can run end to end
            in the browser — a failed payment, a payment link, signed webhooks, a replay that
            changes nothing, and two refused attacks.
          </p>
        </Reveal>

        <Reveal delay={180}>
          <div className={styles.outroActions}>
            <ExternalLink href={links.app} variant="primary">
              Open RevTrace
            </ExternalLink>
            <ExternalLink href={links.source}>View source</ExternalLink>
            <ExternalLink href={links.app} variant="quiet">
              Run the demo
            </ExternalLink>
          </div>
        </Reveal>

        <Reveal delay={240}>
          <div className={styles.outroDisclosure}>
            <Disclosure text={DISCLOSURE} />
          </div>
        </Reveal>

        <Reveal>
          <footer className={styles.footer}>
            <p className={styles.footerLine}>
              Every figure here is transcribed from the project's own generated artifacts and
              is <strong>synthetic</strong>: the population is generated with planted effects,
              so recovering one validates the estimator, not the world. Nothing describes a
              real customer, a real payment, or real money.
            </p>
            <dl className={styles.footerMeta}>
              {[
                ["Experiment", provenance.experimentName],
                ["Label", provenance.label],
                ["Seed", String(provenance.seed)],
              ].map(([k, v]) => (
                <div key={k} className={styles.footerMetaRow}>
                  <dt className="meta">{k}</dt>
                  <dd className={`mono ${styles.footerMetaVal}`}>{v}</dd>
                </div>
              ))}
            </dl>
          </footer>
        </Reveal>
      </div>
    </section>
  );
}
