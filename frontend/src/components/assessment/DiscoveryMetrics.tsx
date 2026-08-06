import React from "react";
import { Box, Typography } from "@mui/material";
import { keyframes } from "@mui/material/styles";
import { colors } from "../../theme";
import { Assessment } from "../../types";
import { AnimatedValue } from "../dashboard/charts/AnimatedValue";
import { useReducedMotion, useSequencedSwap } from "./useAssessmentMotion";

/**
 * One discovered figure at a time.
 *
 * Chips and cards were fighting the animation for attention, so this is bare typography: a single
 * statistic, centred, which counts up and then dissolves into the next. Only one exists at a time
 * (exit-before-enter), so nothing overlaps and the slot never changes height.
 *
 * The values are real scan output and are never invented — the only thing being staged is the order
 * and pace they surface in, so the screen stays curated instead of dumping six numbers at once.
 */

const DWELL_MS = 3400; // how long one statistic holds before dissolving into the next
const EXIT_MS = 340;

const out = keyframes`
  0%   { opacity: 1; transform: translateY(0) scale(1); filter: blur(0px); }
  100% { opacity: 0; transform: translateY(-8px) scale(0.985); filter: blur(5px); }
`;

const enter = keyframes`
  0%   { opacity: 0; transform: translateY(10px) scale(0.99); filter: blur(6px); }
  60%  { opacity: 1; }
  100% { opacity: 1; transform: translateY(0) scale(1); filter: blur(0px); }
`;

interface Metric {
  key: string;
  value: number;
  label: string;
}

function metricsOf(assessment: Assessment): Metric[] {
  const out: Metric[] = [];
  if (assessment.total_resources) {
    out.push({ key: "res", value: assessment.total_resources, label: "Resources discovered" });
  }
  if (assessment.resource_type_count) {
    out.push({ key: "types", value: assessment.resource_type_count, label: "Resource types" });
  }
  for (const entry of assessment.major_resource_types ?? []) {
    if (entry?.count > 0 && out.length < 6) {
      out.push({ key: entry.type, value: entry.count, label: entry.type });
    }
  }
  return out;
}

export default function DiscoveryMetrics({ assessment }: { assessment: Assessment }) {
  const reduced = useReducedMotion();
  const metrics = metricsOf(assessment);
  const [index, setIndex] = React.useState(0);

  // Advance through whatever has actually been discovered so far, looping while the run continues.
  React.useEffect(() => {
    if (metrics.length <= 1) return;
    const t = setInterval(() => setIndex((i) => (i + 1) % metrics.length), DWELL_MS);
    return () => clearInterval(t);
  }, [metrics.length]);

  const current = metrics[Math.min(index, Math.max(0, metrics.length - 1))];
  const { shown, leaving } = useSequencedSwap(current?.key ?? "", EXIT_MS);
  const metric = metrics.find((m) => m.key === shown) ?? current;

  return (
    <Box sx={{ height: 40, display: "flex", alignItems: "center", justifyContent: "center", width: "100%" }}>
      {metric && (
        <Box
          key={metric.key}
          sx={{
            display: "flex",
            alignItems: "baseline",
            gap: 1,
            animation: reduced
              ? "none"
              : leaving
              ? `${out} ${EXIT_MS}ms cubic-bezier(.4,0,1,1) forwards`
              : `${enter} .8s cubic-bezier(.16,1,.3,1)`,
          }}
        >
          <Typography
            component="span"
            sx={{
              fontSize: "1.5rem",
              fontWeight: 700,
              letterSpacing: "-0.02em",
              color: colors.textPrimary,
              fontVariantNumeric: "tabular-nums",
              lineHeight: 1,
            }}
          >
            <AnimatedValue value={metric.value} format={(n) => Math.round(n).toLocaleString()} />
          </Typography>
          <Typography
            component="span"
            sx={{
              fontSize: "0.9375rem",
              color: colors.textMuted,
              letterSpacing: "0.005em",
              lineHeight: 1,
            }}
          >
            {metric.label}
          </Typography>
        </Box>
      )}
    </Box>
  );
}
