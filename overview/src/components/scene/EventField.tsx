/**
 * The hero scene: a field of payment events in flight.
 *
 * Events stream left to right along layered depth bands. Most complete and fade
 * out cool; a minority fail, stall, and drop into the lower band — those are the
 * recovery opportunities the rest of the site is about. The picture is not
 * decoration: the two populations you can see are the two the product reasons
 * over.
 *
 * **Deliberately 2D canvas, not WebGL.** A single-context 2D canvas drawing a
 * few hundred additive sprites costs a fraction of a Three.js runtime in both
 * bundle size and GPU pressure, runs identically on a low-end phone, and needs
 * no fallback path. Depth here is parallax and scale across bands, which reads
 * as spatial without pretending to be a 3D scene. WebGL would be the right call
 * for genuine geometry; this is a particle field.
 *
 * Performance rules it holds to:
 * - stops entirely when scrolled out of view (IntersectionObserver)
 * - stops when the tab is hidden
 * - device-pixel-ratio capped at 2
 * - particle count scales with viewport area
 * - renders a single settled frame and stops under `prefers-reduced-motion`
 */

import { useEffect, useRef } from "react";

import { usePrefersReducedMotion } from "@/lib/motion";
import styles from "./Scene.module.css";

interface Event {
  x: number;
  y: number;
  z: number; // 0 = far, 1 = near
  speed: number;
  failed: boolean;
  /** 0..1, how far through its stall a failed event is. */
  decay: number;
  seed: number;
}

const FAIL_RATE = 0.22;

function makeEvent(w: number, h: number, seeded: boolean): Event {
  const z = Math.random();
  return {
    x: seeded ? Math.random() * w : -20,
    y: h * (0.14 + Math.random() * 0.72),
    z,
    speed: 0.22 + z * 0.85,
    failed: Math.random() < FAIL_RATE,
    decay: 0,
    seed: Math.random() * Math.PI * 2,
  };
}

export function EventField() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const reduced = usePrefersReducedMotion();

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    let width = 0;
    let height = 0;
    let events: Event[] = [];
    let frame = 0;
    let running = false;

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = rect.width;
      height = rect.height;
      canvas.width = Math.max(1, Math.floor(width * dpr));
      canvas.height = Math.max(1, Math.floor(height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      // Count scales with area, so a phone does not render a desktop's field.
      const target = Math.round(Math.min(260, Math.max(70, (width * height) / 5200)));
      events = Array.from({ length: target }, () => makeEvent(width, height, true));
    };

    const draw = (t: number) => {
      ctx.clearRect(0, 0, width, height);
      ctx.globalCompositeOperation = "lighter";

      for (const e of events) {
        const near = 0.35 + e.z * 0.65;

        if (e.failed && e.x > width * 0.52) {
          // Stall: the event stops advancing, sinks, and cools.
          e.decay = Math.min(1, e.decay + 0.012);
          e.y += e.decay * 0.55;
        } else {
          e.x += e.speed * (reduced ? 0 : 1);
        }

        // A slight vertical drift keeps the field from reading as a grid.
        const drift = Math.sin(t * 0.0004 + e.seed) * 5 * e.z;
        const y = e.y + drift;

        const alpha = e.failed
          ? (1 - e.decay) * 0.55 * near + 0.06
          : Math.min(1, (1 - e.x / width) * 1.5) * 0.62 * near;

        const r = (e.failed ? 1.9 : 1.35) * near;

        // Failed events run warm, completed events cool. The hue split is the
        // only information the scene carries, so it does the work.
        const colour = e.failed
          ? `oklch(0.76 0.13 68 / ${alpha})`
          : `oklch(0.82 0.11 190 / ${alpha})`;

        // A short motion trail, drawn as a tapering line rather than many dots.
        const trail = e.failed ? 6 : 15 + e.z * 26;
        const grad = ctx.createLinearGradient(e.x - trail, y, e.x, y);
        grad.addColorStop(0, "oklch(0.8 0.1 200 / 0)");
        grad.addColorStop(1, colour);
        ctx.strokeStyle = grad;
        ctx.lineWidth = r;
        ctx.beginPath();
        ctx.moveTo(e.x - trail, y);
        ctx.lineTo(e.x, y);
        ctx.stroke();

        ctx.fillStyle = colour;
        ctx.beginPath();
        ctx.arc(e.x, y, r, 0, Math.PI * 2);
        ctx.fill();
      }

      ctx.globalCompositeOperation = "source-over";

      // Recycle. Failed events are recycled once fully decayed, so the ratio of
      // visible failures stays stable rather than accumulating.
      for (let i = 0; i < events.length; i++) {
        const e = events[i];
        if (!e) continue;
        if (e.x > width + 40 || (e.failed && e.decay >= 1 && e.y > height)) {
          events[i] = makeEvent(width, height, false);
        }
      }
    };

    const loop = (t: number) => {
      draw(t);
      if (running) frame = requestAnimationFrame(loop);
    };

    const start = () => {
      if (running || reduced) return;
      running = true;
      frame = requestAnimationFrame(loop);
    };
    const stop = () => {
      running = false;
      cancelAnimationFrame(frame);
    };

    resize();
    // Under reduced motion, draw one settled frame and never animate.
    if (reduced) {
      for (const e of events) {
        if (e.failed) e.decay = 0.5;
      }
      draw(0);
    } else {
      start();
    }

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
      resize();
      if (reduced) draw(0);
    };
    window.addEventListener("resize", onResize);

    return () => {
      stop();
      observer.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("resize", onResize);
    };
  }, [reduced]);

  return (
    <canvas
      ref={canvasRef}
      className={styles.eventField}
      aria-hidden
      // Decorative: the meaning it carries is stated in the adjacent copy, so a
      // screen reader gains nothing from a description of moving dots.
    />
  );
}
