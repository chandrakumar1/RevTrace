/**
 * 01 — The problem.
 *
 * The causal chain, laid out as a chain. The fourth link is a question rather
 * than a step, because that is exactly where every recovery dashboard stops and
 * starts guessing.
 */

import { Reveal, Section } from "@/components/ui/Primitives";
import styles from "./Sections.module.css";

const CHAIN = [
  {
    label: "Failed payment",
    detail: "A card is declined, a mandate lapses, a UPI request times out.",
    tone: "unearned",
  },
  {
    label: "Recovery action",
    detail: "A payment link goes out.",
    tone: "plain",
  },
  {
    label: "Customer pays",
    detail: "Every dashboard records a recovery here.",
    tone: "plain",
  },
  {
    label: "Did the action cause it?",
    detail:
      "Some of those customers would have paid anyway. Without an answer, the recovery number is an attribution, not a measurement.",
    tone: "caused",
  },
] as const;

export function Problem() {
  return (
    <Section index="01" eyebrow="The problem" id="problem">
      <Reveal>
        <h2 className="h2" id="problem-heading">
          Gross recovery counts the money.
          <br />
          Incremental recovery counts the <em className={styles.em}>cause.</em>
        </h2>
      </Reveal>

      <Reveal delay={80}>
        <p className={`lede ${styles.sectionLede}`}>
          If you act on a failed payment and the customer pays, you have not learned that you
          caused the payment. You have learned that both things happened.
        </p>
      </Reveal>

      <ol className={styles.chain}>
        {CHAIN.map((step, i) => (
          <Reveal as="li" key={step.label} delay={120 + i * 80}>
            <div className={styles.chainItem} data-tone={step.tone}>
              <span className={`mono ${styles.chainIndex}`}>
                {String(i + 1).padStart(2, "0")}
              </span>
              <div>
                <p className={styles.chainLabel}>{step.label}</p>
                <p className={styles.chainDetail}>{step.detail}</p>
              </div>
            </div>
            {i < CHAIN.length - 1 ? (
              <span className={styles.chainArrow} aria-hidden>
                ↓
              </span>
            ) : null}
          </Reveal>
        ))}
      </ol>
    </Section>
  );
}
