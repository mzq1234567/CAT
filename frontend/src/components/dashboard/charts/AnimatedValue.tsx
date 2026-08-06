import React from "react";

function useReducedMotion(): boolean {
  const [reduced, setReduced] = React.useState(false);
  React.useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    const handler = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener?.("change", handler);
    return () => mq.removeEventListener?.("change", handler);
  }, []);
  return reduced;
}

/**
 * Counts a number up from 0 to `value` on mount (and on value change), formatted with `format`.
 * Uses requestAnimationFrame + easeOutCubic. Honors prefers-reduced-motion (renders final value).
 */
export function AnimatedValue({
  value,
  format,
  duration = 850,
}: {
  value: number;
  format: (n: number) => string;
  duration?: number;
}) {
  const reduced = useReducedMotion();
  // Starts at 0 so the first render counts up; thereafter tracks whatever is currently on screen.
  const [display, setDisplay] = React.useState(0);
  const fromRef = React.useRef(0);

  React.useEffect(() => {
    if (reduced || !isFinite(value)) {
      fromRef.current = value;
      setDisplay(value);
      return;
    }
    // Interpolate from the value CURRENTLY shown, not from zero. When a live counter updates
    // (e.g. resources discovered climbing 100 → 193) it must roll on from where it was; restarting
    // at zero each time reads as a glitch rather than as new data arriving.
    const from = fromRef.current;
    if (from === value) return;
    let raf = 0;
    const start = performance.now();
    let lastPublish = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      const next = from + (value - from) * eased;
      fromRef.current = next;
      // Publish at ~30Hz rather than every frame. Rolling digits are indistinguishable at 30 vs 60,
      // but each publish re-renders the consuming component, and several of these can be counting
      // at once on a busy screen.
      if (now - lastPublish >= 32) {
        lastPublish = now;
        setDisplay(next);
      }
      if (t < 1) {
        raf = requestAnimationFrame(tick);
      } else {
        fromRef.current = value;
        setDisplay(value);
      }
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration, reduced]);

  return <>{format(display)}</>;
}
