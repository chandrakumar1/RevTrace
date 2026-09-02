/**
 * The site's shared UI parts.
 *
 * Small and deliberately few: a section shell, a reveal wrapper, a figure, an
 * external link, and a source line. A page built from twenty bespoke components
 * reads as twenty designs.
 */

import type { CSSProperties, ReactNode } from "react";

import type { Source } from "@/data/evidence";
import { useReveal } from "@/lib/motion";
import styles from "./Primitives.module.css";

/** Fades and lifts its child once, when it enters the viewport. */
export function Reveal({
  children,
  delay = 0,
  as: Tag = "div",
  className,
}: {
  children: ReactNode;
  delay?: number;
  as?: "div" | "li" | "section" | "p";
  className?: string;
}) {
  const ref = useReveal<HTMLDivElement>(delay);
  return (
    <Tag ref={ref as never} className={className}>
      {children}
    </Tag>
  );
}

/**
 * One narrative section.
 *
 * Semantic `<section>` with a labelled heading, so the page is navigable as a
 * document rather than as a scroll surface.
 */
export function Section({
  index,
  eyebrow,
  id,
  children,
}: {
  index: string;
  eyebrow: string;
  id: string;
  children: ReactNode;
}) {
  return (
    <section className="section" id={id} aria-labelledby={`${id}-heading`}>
      <div className="wrap">
        <Reveal>
          <div className="rule">
            <span className="meta">{index}</span>
            <span className="meta">{eyebrow}</span>
          </div>
        </Reveal>
        {children}
      </div>
    </section>
  );
}

export type FigureTone = "caused" | "unearned" | "gross" | "holdout" | "plain" | "refuse";

/**
 * A measured figure.
 *
 * `note` is required rather than optional. Two of the three numbers in the
 * ledger describe the same money under different claims, and a figure shown
 * without saying which claim it supports is the exact confusion this project
 * exists to remove.
 */
export function Figure({
  value,
  label,
  note,
  tone = "plain",
  size = "md",
}: {
  value: string;
  label: string;
  note: string;
  tone?: FigureTone;
  size?: "sm" | "md" | "lg";
}) {
  return (
    <div className={styles.figure} data-tone={tone} data-size={size}>
      <p className="meta">{label}</p>
      <p className={styles.figureValue}>{value}</p>
      <p className={styles.figureNote}>{note}</p>
    </div>
  );
}

/** Where a figure came from, rendered as a quiet trailing line. */
export function SourceLine({ source, extra }: { source: Source; extra?: string }) {
  return (
    <p className={styles.source}>
      <span className="mono">{source.artifact}</span>
      <span aria-hidden> · </span>
      <span className="mono">{source.path}</span>
      {extra ? <> — {extra}</> : null}
    </p>
  );
}

/**
 * A link that leaves the site.
 *
 * Always announces itself: `rel="noreferrer"`, a visually-hidden "opens in a
 * new tab", and an arrow that is decorative only.
 */
export function ExternalLink({
  href,
  children,
  variant = "secondary",
}: {
  href: string;
  children: ReactNode;
  variant?: "primary" | "secondary" | "quiet";
}) {
  return (
    <a
      className={styles.link}
      data-variant={variant}
      href={href}
      target="_blank"
      rel="noreferrer"
    >
      <span>{children}</span>
      <span aria-hidden className={styles.linkArrow}>
        ↗
      </span>
      <span className={styles.srOnly}> (opens in a new tab)</span>
    </a>
  );
}

/** A labelled node in a flow diagram. */
export function Node({
  label,
  detail,
  tone = "plain",
  style,
}: {
  label: string;
  detail?: string;
  tone?: FigureTone;
  style?: CSSProperties;
}) {
  return (
    <div className={styles.node} data-tone={tone} style={style}>
      <p className={styles.nodeLabel}>{label}</p>
      {detail ? <p className={styles.nodeDetail}>{detail}</p> : null}
    </div>
  );
}

/** The permanent synthetic-data disclosure. Not dismissible. */
export function Disclosure({ text }: { text: string }) {
  return (
    <p className={styles.disclosure} role="note">
      <span aria-hidden className={styles.disclosureDot} />
      {text}
    </p>
  );
}
