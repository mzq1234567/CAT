import React from "react";

/** Respects the OS "reduce motion" setting — every animation in this folder gates on it. */
export function useReducedMotion(): boolean {
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
 * Strict exit-before-enter swapping.
 *
 * The outgoing value is held on screen while it animates away; only once that exit has finished is
 * the new value mounted to animate in. Nothing ever overlaps — an earlier crossfade version left the
 * old and new text briefly on top of each other, which read as a rendering fault rather than as a
 * transition. Callers must give the slot a fixed height so the brief gap can't shift the layout.
 */
export function useSequencedSwap<T>(value: T, exitMs: number): { shown: T; leaving: boolean } {
  const [shown, setShown] = React.useState(value);
  const [leaving, setLeaving] = React.useState(false);

  React.useEffect(() => {
    if (value === shown) return;
    setLeaving(true);
    const t = setTimeout(() => {
      setShown(value);
      setLeaving(false);
    }, exitMs);
    return () => clearTimeout(t);
  }, [value, shown, exitMs]);

  return { shown, leaving };
}

/**
 * The backend reports progress in discrete jumps (0 → 15 → 35 → 55 → 70 → 85 → 95 → 100) and the
 * frontend only polls every couple of seconds, so raw progress lurches and then sits frozen.
 *
 * This smooths the bar WITHOUT ever overstating completion. Two rules:
 *   1. It never goes backwards.
 *   2. It never passes the current phase's ceiling — the progress value of the NEXT phase. Inside a
 *      phase it eases asymptotically toward that ceiling and decelerates, so it always looks alive
 *      but can only actually reach the ceiling when the backend confirms the phase really advanced.
 *
 * So the motion is honest: the bar can drift within the band of work known to be in progress, and
 * a real backend transition is what unlocks the next band.
 */
/** How often the eased value is published to React. See the note in `useSmoothProgress`. */
const PUBLISH_INTERVAL_MS = 90;

export function useSmoothProgress(target: number, ceiling: number, reduced: boolean): number {
  const [value, setValue] = React.useState(target);
  const ref = React.useRef(target);
  const lastPublish = React.useRef(0);

  React.useEffect(() => {
    if (reduced) {
      ref.current = Math.max(ref.current, target);
      setValue(ref.current);
      return;
    }
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = Math.min(now - last, 100);
      last = now;
      const current = ref.current;
      // Jump straight to a confirmed backend value if it has overtaken us, then creep toward the
      // ceiling at a rate proportional to the distance left (fast at first, slow near the limit).
      const floor = Math.max(current, target);
      const gap = Math.max(0, ceiling - floor);
      const next = Math.min(ceiling, floor + gap * (1 - Math.exp(-dt / 2600)));
      ref.current = next;

      // Publish at ~11Hz, not once per frame. Setting state every frame re-rendered this component
      // and its whole subtree 60+ times a second, which was the dominant cost on a busy machine —
      // and it bought nothing, because the consumer animates between values with a 1.4s CSS
      // transition. The eased curve is still computed per frame; only the handoff to React is rate
      // limited.
      if (now - lastPublish.current >= PUBLISH_INTERVAL_MS || next >= ceiling - 0.01) {
        lastPublish.current = now;
        if (next !== value) setValue(next);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
    // `value` is intentionally excluded: it changes on publish and would restart the loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, ceiling, reduced]);

  return value;
}
