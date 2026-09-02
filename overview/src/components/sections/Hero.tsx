/**
 * The opening.
 *
 * One claim, one qualification, and the question the whole project exists to
 * answer. No feature list, no metrics, no product screenshot — a visitor should
 * finish this screen understanding the *problem*, and only then be told there is
 * a system for it.
 */

import { EventField } from "@/components/scene/EventField";
import { Disclosure, ExternalLink, Reveal } from "@/components/ui/Primitives";
import { DISCLOSURE, links } from "@/data/evidence";
import styles from "./Sections.module.css";

const QUESTION = [
  "A payment failed.",
  "We intervened.",
  "The customer paid.",
] as const;

export function Hero() {
  return (
    <header className={styles.hero} id="top">
      <EventField />
      <div className={`wrap ${styles.heroInner}`}>
        <Reveal>
          <p className="meta">RevTrace · Razorpay Buildathon · Track 03</p>
        </Reveal>

        <Reveal delay={90}>
          <h1 className={`display ${styles.heroTitle}`}>
            Not every recovered payment
            <br />
            was recovered <em className={styles.em}>because of us.</em>
          </h1>
        </Reveal>

        <Reveal delay={180}>
          <p className={`lede ${styles.heroLede}`}>
            RevTrace measures the revenue a recovery intervention actually caused — not
            merely the money that happened to come back.
          </p>
        </Reveal>

        <Reveal delay={260}>
          <div className={styles.question}>
            {QUESTION.map((line) => (
              <p key={line} className={styles.questionLine}>
                {line}
              </p>
            ))}
            <p className={styles.questionAsk}>Did our intervention cause the payment?</p>
          </div>
        </Reveal>

        <Reveal delay={340}>
          <div className={styles.heroActions}>
            <ExternalLink href={links.app} variant="primary">
              Open RevTrace
            </ExternalLink>
            <ExternalLink href={links.source}>View source</ExternalLink>
          </div>
        </Reveal>

        <Reveal delay={420}>
          <div className={styles.heroDisclosure}>
            <Disclosure text={DISCLOSURE} />
          </div>
        </Reveal>
      </div>

      <div className={styles.scrollHint} aria-hidden>
        <span className="meta">Scroll</span>
        <span className={styles.scrollLine} />
      </div>
    </header>
  );
}
