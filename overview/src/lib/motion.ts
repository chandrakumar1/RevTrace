/**
 * Motion utilities. No animation library — these are the two primitives the
 * site actually needs, and both are a few lines.
 *
 * A dependency would buy timeline orchestration this design does not use: every
 * entrance here is a single transition triggered once when an element enters
 * the viewport, and the scene canvases drive themselves.
 */

import { useEffect, useRef, useState } from "react";

/**
 * Whether the visitor has asked for reduced motion.
 *
 * Read reactively rather than once, because the setting can change while the
 * page is open and a canvas that keeps animating after the preference flips is
 * exactly the thing the preference exists to stop.
 */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return reduced;
}

/**
 * Reveal an element once it enters the viewport.
 *
 * Marks the node with `data-reveal` only after mounting, so the hidden
 * pre-state exists solely when JavaScript is present to remove it again — a
 * failed or blocked script leaves the content readable rather than invisible.
 * The observer disconnects after firing: these are entrances, not toggles, and
 * content that fades out again when scrolled past is an irritation.
 */
export function useReveal<T extends HTMLElement>(delayMs = 0) {
  const ref = useRef<T | null>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;

    if (delayMs > 0) node.style.setProperty("--reveal-delay", `${delayMs}ms`);
    node.setAttribute("data-reveal", "");

    if (typeof IntersectionObserver === "undefined") {
      node.setAttribute("data-reveal", "shown");
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            node.setAttribute("data-reveal", "shown");
            observer.disconnect();
          }
        }
      },
      // No negative bottom margin, deliberately. Shrinking the root to make
      // reveals feel "settled" also means anything sitting low in the *first*
      // viewport never qualifies — which left the hero's call-to-action links
      // permanently faded out on short screens. A plain root with a low
      // threshold reveals on-screen content immediately and still staggers
      // everything further down the page.
      { threshold: 0.1 },
    );

    observer.observe(node);
    return () => observer.disconnect();
  }, [delayMs]);

  return ref;
}

/**
 * A 0→1 progress value for how far an element has travelled through the
 * viewport, for parallax and scene choreography.
 *
 * Updates on `requestAnimationFrame` while the element is visible and stops
 * entirely when it is not, so an off-screen section costs nothing. Returns a
 * constant `1` under reduced motion — the scene renders its settled state
 * instead of tracking the scroll.
 */
export function useScrollProgress<T extends HTMLElement>(enabled = true) {
  const ref = useRef<T | null>(null);
  const [progress, setProgress] = useState(enabled ? 0 : 1);

  useEffect(() => {
    if (!enabled) {
      setProgress(1);
      return;
    }
    const node = ref.current;
    if (!node) return;

    let frame = 0;
    let visible = false;

    const measure = () => {
      const rect = node.getBoundingClientRect();
      const vh = window.innerHeight || 1;
      // 0 when the element's top reaches the bottom of the viewport,
      // 1 when its bottom reaches the top.
      const raw = (vh - rect.top) / (vh + rect.height);
      setProgress(Math.min(1, Math.max(0, raw)));
      if (visible) frame = requestAnimationFrame(measure);
    };

    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        visible = entry.isIntersecting;
        if (visible) {
          cancelAnimationFrame(frame);
          frame = requestAnimationFrame(measure);
        } else {
          cancelAnimationFrame(frame);
        }
      }
    });

    observer.observe(node);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [enabled]);

  return { ref, progress };
}
