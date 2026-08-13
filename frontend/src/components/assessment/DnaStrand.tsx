import React from "react";
import { Box } from "@mui/material";
import { useReducedMotion } from "./useAssessmentMotion";

/**
 * A horizontal double-strand data helix — the hero of the running-assessment screen.
 *
 * Two substantial glowing strands sweep left→right as sine waves a half-phase apart, so they
 * continuously converge, cross and separate. Connecting rungs shrink to nothing at each crossing and
 * open out again; a front/back depth cue (the nearer strand is brighter and thicker, drawn last) reads
 * as the structure twisting around its horizontal axis — no 3-D engine. Each strand is drawn as a soft
 * glow pass plus a crisp depth-shaded core, so it has real visual weight without neon.
 *
 * A restrained telemetry-particle system rides the strands and occasionally detaches, drifting downward
 * and fading — "data fragments", not stars. Palette is product-aligned: Azure blue → cyan → teal with an
 * occasional restrained violet. One <canvas> (GPU-composited), rAF-driven, DPR-capped, ResizeObserver
 * sized, cleaned up on unmount, and a calm static frame under prefers-reduced-motion.
 */

const BLUE: [number, number, number] = [11, 99, 229];
const CYAN: [number, number, number] = [49, 216, 255];
const TEAL: [number, number, number] = [0, 224, 198];
const VIOLET: [number, number, number] = [122, 92, 255];

// Colour along the width: blue → cyan → teal, with a whisper of violet at the far end only.
function strandColor(t: number): [number, number, number] {
  const stops: [number, [number, number, number]][] = [
    [0, BLUE], [0.4, CYAN], [0.72, TEAL], [1, VIOLET],
  ];
  const clamped = Math.max(0, Math.min(1, t));
  for (let i = 0; i < stops.length - 1; i++) {
    const [t0, c0] = stops[i];
    const [t1, c1] = stops[i + 1];
    if (clamped <= t1) {
      const f = (clamped - t0) / (t1 - t0 || 1);
      return [c0[0] + (c1[0] - c0[0]) * f, c0[1] + (c1[1] - c0[1]) * f, c0[2] + (c1[2] - c0[2]) * f];
    }
  }
  return VIOLET;
}

function particleColor(): [number, number, number] {
  const r = Math.random();
  if (r < 0.42) return CYAN;
  if (r < 0.72) return BLUE;
  if (r < 0.9) return TEAL;
  return VIOLET; // occasional, restrained
}

interface Rider { t: number; speed: number; strand: 0 | 1; size: number }
interface Drifter { x: number; y: number; vx: number; vy: number; life: number; max: number; size: number; color: [number, number, number] }

function DnaStrand() {
  const reduced = useReducedMotion();
  const wrapRef = React.useRef<HTMLDivElement | null>(null);
  const canvasRef = React.useRef<HTMLCanvasElement | null>(null);

  React.useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let cssW = 0;
    let cssH = 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    const resize = () => {
      cssW = wrap.clientWidth;
      cssH = Math.max(190, Math.round(cssW * 0.32));
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
      canvas.style.height = `${cssH}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const riders: Rider[] = Array.from({ length: 7 }, (_, i) => ({
      t: (i / 7 + 0.05) % 1,
      speed: 0.05 + (i % 3) * 0.018,
      strand: (i % 2) as 0 | 1,
      size: i % 3 === 0 ? 3 : 2.4,
    }));

    const drifters: Drifter[] = [];
    const DRIFTERS = 18;

    const midY = () => cssH / 2;
    const amp = () => cssH * 0.34;
    const cycles = 2.3;
    const f = () => (Math.PI * 2 * cycles) / cssW;
    let phase = 0;

    const yOf = (x: number, s: number) => midY() + amp() * Math.sin(x * f() + phase + s * Math.PI);
    const depthOf = (x: number, s: number) => Math.cos(x * f() + phase + s * Math.PI); // +1 front, −1 back

    const spawnDrifter = (d: Drifter) => {
      const s = Math.random() < 0.5 ? 0 : 1;
      const x = Math.random() * cssW;
      d.x = x;
      d.y = yOf(x, s);
      d.vx = (Math.random() - 0.5) * 10;
      d.vy = 10 + Math.random() * 26; // gentle downward drift
      d.max = 1.5 + Math.random() * 2.4;
      d.life = d.max;
      d.size = 0.8 + Math.random() * 1.7;
      d.color = particleColor();
    };
    for (let i = 0; i < DRIFTERS; i++) {
      const d: Drifter = { x: 0, y: 0, vx: 0, vy: 0, life: 0, max: 1, size: 1, color: CYAN };
      spawnDrifter(d);
      d.life = Math.random() * d.max; // stagger so they don't all fade together
      drifters.push(d);
    }

    // Build the two strands' sampled points for this frame (shared by glow + core + particles).
    const strandPoints = (s: number, steps: number) => {
      const pts: [number, number][] = [];
      for (let i = 0; i <= steps; i++) {
        const x = (i / steps) * cssW;
        pts.push([x, yOf(x, s)]);
      }
      return pts;
    };

    const drawStrand = (s: number) => {
      const steps = Math.max(56, Math.round(cssW / 8));
      const pts = strandPoints(s, steps);

      // Glow pass — one soft wide stroke with a gradient along the width.
      const grad = ctx.createLinearGradient(0, 0, cssW, 0);
      const g0 = strandColor(0), g1 = strandColor(0.45), g2 = strandColor(0.75), g3 = strandColor(1);
      grad.addColorStop(0, `rgba(${g0[0]},${g0[1]},${g0[2]},0.10)`);
      grad.addColorStop(0.45, `rgba(${g1[0]},${g1[1]},${g1[2]},0.14)`);
      grad.addColorStop(0.75, `rgba(${g2[0]},${g2[1]},${g2[2]},0.14)`);
      grad.addColorStop(1, `rgba(${g3[0]},${g3[1]},${g3[2]},0.10)`);
      ctx.strokeStyle = grad;
      ctx.lineWidth = Math.max(8, cssH * 0.055);
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      pts.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
      ctx.stroke();

      // Core pass — per segment, so colour follows x and width/alpha follow depth (the 3-D cue).
      for (let i = 1; i < pts.length; i++) {
        const [x0, y0] = pts[i - 1];
        const [x1, y1] = pts[i];
        const mid = (x0 + x1) / 2;
        const front = (depthOf(mid, s) + 1) / 2; // 0 back .. 1 front
        const [cr, cg, cb] = strandColor(mid / cssW);
        ctx.strokeStyle = `rgba(${cr},${cg},${cb},${0.4 + front * 0.55})`;
        ctx.lineWidth = 2.4 + front * 3.4; // substantially thicker than before
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.lineTo(x1, y1);
        ctx.stroke();
      }
    };

    const drawRungs = () => {
      const rungCount = Math.round(cycles * 8);
      for (let r = 0; r <= rungCount; r++) {
        const x = (r / rungCount) * cssW;
        const y0 = yOf(x, 0);
        const y1 = yOf(x, 1);
        const spread = Math.abs(y0 - y1) / (2 * amp());
        const [cr, cg, cb] = strandColor(x / cssW);
        ctx.strokeStyle = `rgba(${cr},${cg},${cb},${0.05 + spread * 0.18})`;
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.moveTo(x, y0);
        ctx.lineTo(x, y1);
        ctx.stroke();
        for (const s of [0, 1]) {
          const y = s === 0 ? y0 : y1;
          const front = (depthOf(x, s) + 1) / 2;
          ctx.beginPath();
          ctx.arc(x, y, 1.8 + front * 1.8, 0, Math.PI * 2);
          ctx.fillStyle = `rgba(${cr},${cg},${cb},${0.2 + front * 0.55})`;
          ctx.fill();
        }
      }
    };

    const drawRider = (p: Rider) => {
      const px = p.t * cssW;
      const py = yOf(px, p.strand);
      const front = (depthOf(px, p.strand) + 1) / 2;
      const [cr, cg, cb] = strandColor(p.t);
      const glow = ctx.createRadialGradient(px, py, 0, px, py, p.size * 3.6);
      glow.addColorStop(0, `rgba(${cr},${cg},${cb},${0.5 + front * 0.4})`);
      glow.addColorStop(1, `rgba(${cr},${cg},${cb},0)`);
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(px, py, p.size * 3.6, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(px, py, p.size * (0.7 + front * 0.5), 0, Math.PI * 2);
      ctx.fillStyle = `rgba(255,255,255,${0.55 + front * 0.4})`;
      ctx.fill();
    };

    const drawDrifter = (d: Drifter) => {
      const r = Math.max(0, d.life / d.max);
      // fade in over the first 18%, fade out over the last 45%
      const alpha = Math.min(1, (1 - r) / 0.18) * Math.min(1, r / 0.45) * 0.85;
      const [cr, cg, cb] = d.color;
      ctx.beginPath();
      ctx.arc(d.x, d.y, d.size, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${cr},${cg},${cb},${alpha})`;
      ctx.fill();
    };

    const drawFrame = (dt: number, motion: number) => {
      phase += dt * 0.85 * motion;
      ctx.clearRect(0, 0, cssW, cssH);

      drawRungs();
      // Back strand first, front strand last, so the nearer one overlaps at crossings.
      const frontFirst = Math.sin(phase) >= 0 ? [1, 0] : [0, 1];
      drawStrand(frontFirst[0]);
      drawStrand(frontFirst[1]);

      for (const p of riders) {
        if (motion > 0) {
          p.t = (p.t + p.speed * dt * motion) % 1;
          if (Math.random() < 0.004 && drifters.length) {
            // occasionally spawn a drifter at this rider's position (a fragment detaching)
            const slot = drifters[Math.floor(Math.random() * drifters.length)];
            if (slot.life < slot.max * 0.15) {
              slot.x = p.t * cssW;
              slot.y = yOf(slot.x, p.strand);
            }
          }
        }
        drawRider(p);
      }

      for (const d of drifters) {
        if (motion > 0) {
          d.x += d.vx * dt;
          d.y += d.vy * dt;
          d.vy += 10 * dt; // slight gravity
          d.life -= dt;
          if (d.life <= 0 || d.y > cssH + 12) spawnDrifter(d);
        }
        drawDrifter(d);
      }
    };

    let raf = 0;
    let last = performance.now();
    const loop = (now: number) => {
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      drawFrame(dt, 1);
      raf = requestAnimationFrame(loop);
    };

    const ro = new ResizeObserver(() => {
      resize();
      if (reduced) {
        phase = 0.7;
        drawFrame(0, 0);
      }
    });
    ro.observe(wrap);

    resize();
    if (reduced) {
      phase = 0.7;
      drawFrame(0, 0); // calm static helix
    } else {
      last = performance.now();
      raf = requestAnimationFrame(loop);
    }

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [reduced]);

  return (
    <Box ref={wrapRef} sx={{ width: "100%", lineHeight: 0 }}>
      <Box
        component="canvas"
        ref={canvasRef}
        role="img"
        aria-label="Analysing your Azure environment"
        sx={{ width: "100%", display: "block" }}
      />
    </Box>
  );
}

export default React.memo(DnaStrand);
