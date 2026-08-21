import React from "react";
import { Box, Typography } from "@mui/material";
import { alpha, keyframes } from "@mui/material/styles";
import { colors } from "../../theme";
import { Assessment } from "../../types";
import { useThemeMode } from "../themeMode";
import { useReducedMotion } from "./useAssessmentMotion";

/**
 * A few resource "nodes" drifting around the flow, labelled with the environment being assessed.
 *
 * The labels are REAL: once discovery returns resource types they are the customer's own types; before
 * that, they are neutral Azure resource categories used purely as placeholders (never a fabricated count
 * or finding). Only a handful are visible at once and they cross-fade slowly, so the periphery feels
 * alive without pulling attention from the caption. Pure CSS/opacity; a calm static set under
 * prefers-reduced-motion; hidden on narrow viewports where they'd crowd the composition.
 */

const PLACEHOLDERS = [
  "Virtual Machines", "Storage Accounts", "Virtual Networks", "Managed Disks",
  "Public IP Addresses", "App Services", "SQL Databases", "Load Balancers",
];

function labelsFor(assessment: Assessment): string[] {
  const real = (assessment.major_resource_types ?? [])
    .map((t) => t?.type)
    .filter((t): t is string => Boolean(t));
  return real.length >= 3 ? real : PLACEHOLDERS;
}

// Peripheral anchor points around the horizontal flow — deliberately off the centre line.
const SLOTS = [
  { top: "4%", left: "2%" },
  { top: "12%", right: "3%" },
  { bottom: "10%", left: "8%" },
  { bottom: "2%", right: "10%" },
];

const fade = keyframes`
  0%   { opacity: 0; transform: translateY(6px); }
  18%  { opacity: 1; transform: none; }
  82%  { opacity: 1; transform: none; }
  100% { opacity: 0; transform: translateY(-6px); }
`;

function Pill({ label }: { label: string }) {
  return (
    <Box
      sx={{
        display: "inline-flex",
        alignItems: "center",
        gap: 0.75,
        px: 1.25,
        py: 0.55,
        borderRadius: 2,
        bgcolor: colors.surface,
        border: `1px solid ${colors.border}`,
        boxShadow: "0 4px 14px rgba(16,24,40,0.06)",
        whiteSpace: "nowrap",
      }}
    >
      <Box sx={{ width: 6, height: 6, borderRadius: "50%", bgcolor: colors.accentBlue, flexShrink: 0 }} />
      <Typography variant="caption" sx={{ fontWeight: 600, color: colors.textSecondary, lineHeight: 1 }}>
        {label}
      </Typography>
    </Box>
  );
}

function Slot({
  labels,
  offset,
  position,
  reduced,
}: {
  labels: string[];
  offset: number;
  position: React.CSSProperties;
  reduced: boolean;
}) {
  const [tick, setTick] = React.useState(0);
  const CYCLE = 4200;

  React.useEffect(() => {
    if (reduced || labels.length <= SLOTS.length) return;
    const t = setInterval(() => setTick((v) => v + 1), CYCLE);
    return () => clearInterval(t);
  }, [reduced, labels.length]);

  const label = labels[(tick * SLOTS.length + offset) % labels.length];

  return (
    <Box sx={{ position: "absolute", ...position }}>
      <Box
        key={label}
        sx={{ animation: reduced ? "none" : `${fade} ${CYCLE}ms ease-in-out ${offset * 500}ms infinite` }}
      >
        <Pill label={label} />
      </Box>
    </Box>
  );
}

function ResourceNodes({ assessment }: { assessment: Assessment }) {
  const reduced = useReducedMotion();
  // Subscribe to the theme mode so a theme SWITCH re-renders the pills (their `colors.*` styles refresh);
  // context-driven re-renders bypass the memo comparator below, which only gates prop-driven ones.
  const { mode } = useThemeMode();
  const labels = labelsFor(assessment);
  const slots = SLOTS.slice(0, Math.min(SLOTS.length, labels.length));

  return (
    <Box
      aria-hidden
      data-theme-mode={mode}
      sx={{
        position: "absolute",
        inset: 0,
        pointerEvents: "none",
        display: { xs: "none", md: "block" }, // avoid crowding narrow viewports
        zIndex: 2,
      }}
    >
      {slots.map((pos, i) => (
        <Slot key={i} labels={labels} offset={i} position={pos as React.CSSProperties} reduced={reduced} />
      ))}
    </Box>
  );
}

// Re-render only when the discovered label set could have changed (keeps the periphery cheap while the
// parent re-renders on every poll/progress publish).
export default React.memo(ResourceNodes, (a, b) => {
  const at = a.assessment.major_resource_types?.length ?? 0;
  const bt = b.assessment.major_resource_types?.length ?? 0;
  return at === bt;
});
