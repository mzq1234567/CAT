import React from "react";
import { Box } from "@mui/material";
import { useReducedMotion } from "./useAssessmentMotion";

/**
 * The centrepiece: an abstract flow, not an object.
 *
 * Six long curved paths sweep the full width, drawing together through a narrow waist at the centre
 * and opening out again. Particles ride those exact paths, so material visibly converges, passes
 * through the constriction, and disperses — reading as "work flowing through something" without any
 * diagram, icon, box or label to explain.
 *
 * Deliberately NOT a large central object: the subject is the movement itself. There is no engine
 * shape, no sphere, nothing static holding the middle.
 *
 * Seamlessness comes from negative animation delays — every particle starts mid-cycle, so the stream
 * is already full at the first frame and has no beginning or end. Path durations are mutually
 * non-harmonic (6.7 / 7.3 / 7.9 / 8.6 / 9.2 / 10.1s), so the composition never falls into a visible
 * repeat no matter how long it runs.
 *
 * Pure CSS transforms and `offset-path` — no per-frame JavaScript. Renders as a still under
 * prefers-reduced-motion.
 */

// Left edge → waist at (280,100) → right edge. Off-canvas endpoints so nothing pops in or out.
const PATHS = [
  { d: "M -30 18 C 130 24, 220 74, 285 100 C 350 126, 430 178, 590 186", dur: 8.6, n: 3 },
  { d: "M -30 62 C 130 64, 225 88, 285 100 C 345 112, 440 140, 590 146", dur: 7.3, n: 3 },
  { d: "M -30 100 C 120 100, 220 100, 285 100 C 350 100, 450 100, 590 100", dur: 6.7, n: 4 },
  { d: "M -30 138 C 130 136, 225 112, 285 100 C 345 88, 440 60, 590 54", dur: 7.9, n: 3 },
  { d: "M -30 182 C 130 176, 220 126, 285 100 C 350 74, 430 22, 590 14", dur: 9.2, n: 3 },
  { d: "M -30 210 C 150 200, 230 130, 285 100 C 340 70, 420 4, 590 -10", dur: 10.1, n: 2 },
];

const ACCENTS = ["#31D8FF", "#0B63E5", "#59B7FF", "#00E0C6", "#7A5CFF", "#31D8FF"];

// A far layer of ambient motes drifting behind the flow. Smaller, dimmer and much slower than the
// particles on the paths — the speed difference is what creates parallax depth rather than a second
// scatter of dots on the same plane. Fixed positions so re-renders never reshuffle them.
const MOTES = [
  { x: 78, y: 44, r: 1.4, dur: 26, delay: -3, dx: 14, dy: -10 },
  { x: 168, y: 158, r: 1.1, dur: 31, delay: -11, dx: -12, dy: -14 },
  { x: 262, y: 26, r: 1.6, dur: 24, delay: -7, dx: 10, dy: 12 },
  { x: 352, y: 172, r: 1.2, dur: 35, delay: -19, dx: 16, dy: -8 },
  { x: 452, y: 58, r: 1.5, dur: 28, delay: -14, dx: -14, dy: 12 },
  { x: 508, y: 140, r: 1.1, dur: 33, delay: -5, dx: 12, dy: -12 },
  { x: 122, y: 108, r: 1.3, dur: 29, delay: -22, dx: -10, dy: 14 },
  { x: 418, y: 118, r: 1.2, dur: 37, delay: -9, dx: 14, dy: 10 },
];

const CSS = `
@keyframes ff-ride {
  0%   { offset-distance: 0%;   opacity: 0; }
  9%   { opacity: var(--peak); }
  88%  { opacity: var(--peak); }
  100% { offset-distance: 100%; opacity: 0; }
}
/* The waist breathes very slightly, so the convergence point is never perfectly still. */
@keyframes ff-waist {
  0%, 100% { opacity: 0.28; transform: scale(0.94); }
  50%      { opacity: 0.5;  transform: scale(1.06); }
}
/* A faint luminance travels the guide lines themselves. */
@keyframes ff-shimmer {
  0%   { stroke-dashoffset: 620; }
  100% { stroke-dashoffset: 0; }
}
/* Occasional brighter pulse down a line — infrequent, so it reads as an event, not a rhythm. */
@keyframes ff-pulse {
  0%   { stroke-dashoffset: 620; opacity: 0; }
  8%   { opacity: 0.85; }
  34%  { opacity: 0.85; }
  46%  { stroke-dashoffset: 0; opacity: 0; }
  100% { stroke-dashoffset: 0; opacity: 0; }
}
/* Far-layer motes: slow parallax drift. */
@keyframes ff-drift {
  0%   { transform: translate(0, 0); opacity: 0; }
  22%  { opacity: var(--peak); }
  78%  { opacity: var(--peak); }
  100% { transform: translate(var(--dx), var(--dy)); opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .ff-particle { display: none; }
  [class^="ff-"] { animation: none !important; }
}
`;

/**
 * Memoised: this renders ~40 animated SVG nodes and depends only on `width`, but it sits under a
 * component that re-renders on every progress publish and every poll. Without this, React
 * reconciled the entire field on each of those — measurably the largest cost on a throttled CPU.
 */
function FlowField({ width = 560 }: { width?: number }) {
  const reduced = useReducedMotion();

  return (
    <Box
      component="svg"
      viewBox="0 0 560 200"
      role="img"
      aria-label="Analysing your Azure environment"
      sx={{ width, maxWidth: "100%", height: "auto", display: "block", overflow: "visible" }}
    >
      <style>{CSS}</style>

      <defs>
        <linearGradient id="ff-guide" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="#0B63E5" stopOpacity="0" />
          <stop offset="30%" stopColor="#31D8FF" stopOpacity="0.28" />
          <stop offset="70%" stopColor="#31D8FF" stopOpacity="0.28" />
          <stop offset="100%" stopColor="#0B63E5" stopOpacity="0" />
        </linearGradient>
        <radialGradient id="ff-waist" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#31D8FF" stopOpacity="0.55" />
          <stop offset="100%" stopColor="#31D8FF" stopOpacity="0" />
        </radialGradient>
      </defs>

      {/* Far layer: ambient motes drifting slowly behind everything (parallax depth) */}
      {!reduced &&
        MOTES.map((m, i) => (
          <circle
            key={`m-${i}`}
            className="ff-mote"
            cx={m.x}
            cy={m.y}
            r={m.r}
            fill="#7FB4E8"
            style={
              {
                "--dx": `${m.dx}px`,
                "--dy": `${m.dy}px`,
                "--peak": 0.32,
                opacity: 0,
                animation: `ff-drift ${m.dur}s ease-in-out ${m.delay}s infinite`,
              } as React.CSSProperties
            }
          />
        ))}

      {/* Base curves — continuous and faint, so the flow reads as paths rather than as loose dots.
          (Dashing THIS stroke instead of overlaying the shimmer turns the curves into disconnected
          scratches and the particles lose the line they are supposed to be riding.) */}
      {PATHS.map((p, i) => (
        <path
          key={`b-${i}`}
          d={p.d}
          fill="none"
          stroke="#0B63E5"
          strokeOpacity="0.12"
          strokeWidth="1"
          strokeLinecap="round"
        />
      ))}

      {/* A separate luminance travelling the length of each curve */}
      {!reduced &&
        PATHS.map((p, i) => (
          <path
            key={`s-${i}`}
            className="ff-guide"
            d={p.d}
            fill="none"
            stroke="url(#ff-guide)"
            strokeWidth="1.4"
            strokeLinecap="round"
            strokeDasharray="80 560"
            style={{ animation: `ff-shimmer ${p.dur * 2.2}s linear ${-i * 1.9}s infinite` }}
          />
        ))}

      {/* Occasional bright pulse running a single line — long, offset cycles so two rarely coincide */}
      {!reduced &&
        PATHS.map((p, i) => (
          <path
            key={`pulse-${i}`}
            className="ff-pulse"
            d={p.d}
            fill="none"
            stroke="#7FE9FF"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeDasharray="46 594"
            style={{
              opacity: 0,
              animation: `ff-pulse ${17 + i * 2.6}s cubic-bezier(.45,0,.55,1) ${-i * 4.3}s infinite`,
            }}
          />
        ))}

      {/* Soft luminance at the waist — light, not an object */}
      <ellipse
        className="ff-waist"
        cx="285"
        cy="100"
        rx="66"
        ry="46"
        fill="url(#ff-waist)"
        style={{
          transformBox: "view-box",
          transformOrigin: "285px 100px",
          ...(reduced ? {} : { animation: "ff-waist 6.5s ease-in-out infinite" }),
        }}
      />

      {/* Particles riding the exact guide geometry */}
      {!reduced &&
        PATHS.flatMap((p, i) =>
          Array.from({ length: p.n }, (_, k) => {
            // Negative, evenly spaced delays: the stream is already full on the first frame.
            const delay = -(p.dur / p.n) * k - i * 0.31;
            const r = k % 3 === 0 ? 2.6 : k % 3 === 1 ? 1.9 : 2.2;
            return (
              <circle
                key={`p-${i}-${k}`}
                className="ff-particle"
                r={r}
                fill={ACCENTS[i % ACCENTS.length]}
                style={
                  {
                    offsetPath: `path("${p.d}")`,
                    offsetRotate: "0deg",
                    // offset-anchor follows transform-origin; SVG resolves that against the viewBox
                    // unless the box is pinned to the shape, which throws particles off their path.
                    transformBox: "fill-box",
                    transformOrigin: "center",
                    "--peak": k % 3 === 1 ? 0.55 : 0.9,
                    opacity: 0,
                    animation: `ff-ride ${p.dur}s linear ${delay}s infinite`,
                  } as React.CSSProperties
                }
              />
            );
          }),
        )}
    </Box>
  );
}

export default React.memo(FlowField);
