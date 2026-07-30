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
  const [display, setDisplay] = React.useState(value);

  React.useEffect(() => {
    if (reduced || !isFinite(value)) {
      setDisplay(value);
      return;
    }
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      setDisplay(value * eased);
      if (t < 1) raf = requestAnimationFrame(tick);
      else setDisplay(value);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration, reduced]);

  return <>{format(display)}</>;
}
