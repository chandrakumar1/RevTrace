/**
 * The randomisation scene: one population divides into two.
 *
 * 10,000 units enter as a single field and separate into treatment and holdout.
 * The split is driven by scroll progress, so the reader performs the
 * randomisation by moving down the page — which is the point of the section.
 *
 * The counts are **not** drawn here; they are rendered as text beside the
 * canvas, where they can be read, selected and checked against the artifact. A
 * number that exists only as pixels inside a canvas is a number nobody can
 * verify.
 *
 * Represents units at 1:50 — drawing ten thousand sprites would cost far more
 * than it communicates, and the ratio is what the picture is for. The scale is
 * stated in the caption rather than implied.
 */

import { useEffect, useRef } from "react";

import { usePrefersReducedMotion } from "@/lib/motion";
import styles from "./Scene.module.css";

/** One drawn dot per this many experimental units. Stated in the caption. */
export const UNITS_PER_DOT = 50;

interface Unit {
  /** Start position, in normalised space. */
  x0: number;
  y0: number;
  /** Destination once assigned. */
  x1: number;
  y1: number;
  treated: boolean;
  seed: number;
}

export function SplitField({
  progress,
  treatment,
  holdout,
}: {
  /** 0 → single population, 1 → fully separated. */
  progress: number;
  treatment: number;
  holdout: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const reduced = usePrefersReducedMotion();
  const progressRef = useRef(progress);
  progressRef.current = reduced ? 1 : progress;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    let width = 0;
    let height = 0;
    let units: Unit[] = [];
    let frame = 0;
    let running = false;

    const build = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = rect.width;
      height = rect.height;
      canvas.width = Math.max(1, Math.floor(width * dpr));
      canvas.height = Math.max(1, Math.floor(height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const nTreat = Math.round(treatment / UNITS_PER_DOT);
      const nHold = Math.round(holdout / UNITS_PER_DOT);
      const total = nTreat + nHold;

      units = Array.from({ length: total }, (_, i) => {
        const treated = i < nTreat;
        // Start: one interleaved cloud, so the two arms are indistinguishable
        // before assignment — which is what randomisation means.
        const a = Math.random() * Math.PI * 2;
        const rad = Math.sqrt(Math.random());
        // End: two columns. Treatment above the midline, holdout below.
        const indexInArm = treated ? i : i - nTreat;
        const perRow = Math.ceil(Math.sqrt(treated ? nTreat : nHold) * 1.9);
        const row = Math.floor(indexInArm / perRow);
        const col = indexInArm % perRow;
        const rows = Math.ceil((treated ? nTreat : nHold) / perRow);

        return {
          x0: 0.5 + Math.cos(a) * rad * 0.3,
          y0: 0.5 + Math.sin(a) * rad * 0.42,
          x1: 0.08 + (col / Math.max(1, perRow - 1)) * 0.84,
          y1: treated
            ? 0.1 + (row / Math.max(1, rows - 1 || 1)) * 0.3
            : 0.6 + (row / Math.max(1, rows - 1 || 1)) * 0.3,
          treated,
          seed: Math.random() * Math.PI * 2,
        };
      });
    };

    // Ease so the separation feels decisive rather than linear.
    const ease = (p: number) => 1 - Math.pow(1 - p, 3);

    const draw = (t: number) => {
      const p = ease(Math.min(1, Math.max(0, progressRef.current)));
      ctx.clearRect(0, 0, width, height);
      ctx.globalCompositeOperation = "lighter";

      for (const u of units) {
        const drift = reduced ? 0 : Math.sin(t * 0.0006 + u.seed) * 1.6 * (1 - p);
        const x = (u.x0 + (u.x1 - u.x0) * p) * width;
        const y = (u.y0 + (u.y1 - u.y0) * p) * height + drift;

        // Colour resolves only as the arms separate: before assignment the
        // units genuinely are the same population.
        const colour = u.treated
          ? `oklch(0.82 ${0.02 + 0.13 * p} 172 / ${0.35 + 0.4 * p})`
          : `oklch(0.68 ${0.02 + 0.08 * p} 285 / ${0.3 + 0.35 * p})`;

        ctx.fillStyle = colour;
        ctx.beginPath();
        ctx.arc(x, y, 1.5 + p * 0.5, 0, Math.PI * 2);
        ctx.fill();
      }

      ctx.globalCompositeOperation = "source-over";

      // The dividing line appears as the split resolves.
      if (p > 0.15) {
        ctx.strokeStyle = `oklch(0.4 0.02 265 / ${(p - 0.15) * 0.9})`;
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 5]);
        ctx.beginPath();
        ctx.moveTo(0, height * 0.5);
        ctx.lineTo(width, height * 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    };

    const loop = (t: number) => {
      draw(t);
      if (running) frame = requestAnimationFrame(loop);
    };
    const start = () => {
      if (running) return;
      running = true;
      frame = requestAnimationFrame(loop);
    };
    const stop = () => {
      running = false;
      cancelAnimationFrame(frame);
    };

    build();
    draw(0);

    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) start();
        else stop();
      }
    });
    observer.observe(canvas);

    const onVisibility = () => (document.hidden ? stop() : start());
    document.addEventListener("visibilitychange", onVisibility);
    const onResize = () => {
      build();
      draw(0);
    };
    window.addEventListener("resize", onResize);

    return () => {
      stop();
      observer.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("resize", onResize);
    };
  }, [treatment, holdout, reduced]);

  return <canvas ref={canvasRef} className={styles.splitField} aria-hidden />;
}
