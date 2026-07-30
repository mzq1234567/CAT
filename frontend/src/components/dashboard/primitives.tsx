import React from "react";
import { Box, Card, CardContent, Tooltip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { colors } from "../../theme";
import { useMounted } from "./charts/useMounted";

/** Numbered section header that gives the report its consulting-story cadence. */
export function SectionHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
}) {
  return (
    <Box
      display="flex"
      justifyContent="space-between"
      alignItems="flex-end"
      flexWrap="wrap"
      gap={2}
      mb={2}
    >
      <Box>
        <Typography variant="h6" fontWeight={700} color={colors.textPrimary} lineHeight={1.2}>
          {title}
        </Typography>
        {subtitle && (
          <Typography variant="body2" color="text.secondary" mt={0.25}>
            {subtitle}
          </Typography>
        )}
      </Box>
      {action}
    </Box>
  );
}

/** Executive KPI tile. `emphasis` gives the headline number a colored wash + ring. */
export function StatTile({
  label,
  value,
  sub,
  icon,
  color,
  emphasis = false,
}: {
  label: string;
  value: React.ReactNode;
  sub?: string;
  icon?: React.ReactNode;
  color: string;
  emphasis?: boolean;
}) {
  return (
    <Card
      sx={{
        height: "100%",
        transition: "transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease",
        "&:hover": {
          transform: "translateY(-3px)",
          boxShadow: `0 10px 26px ${alpha(color, 0.18)}`,
          borderColor: alpha(color, 0.5),
        },
        ...(emphasis && {
          background: `linear-gradient(160deg, ${alpha(color, 0.16)} 0%, ${alpha(color, 0.04)} 60%)`,
          borderColor: alpha(color, 0.4),
        }),
      }}
    >
      <CardContent sx={{ p: 2.5, "&:last-child": { pb: 2.5 } }}>
        <Box display="flex" alignItems="center" justifyContent="space-between" mb={1.25}>
          <Typography variant="body2" color="text.secondary" fontWeight={500}>
            {label}
          </Typography>
          {icon && (
            <Box
              sx={{
                bgcolor: alpha(color, 0.15),
                border: `1px solid ${alpha(color, 0.3)}`,
                borderRadius: 2,
                p: 0.8,
                display: "flex",
                color,
              }}
            >
              {icon}
            </Box>
          )}
        </Box>
        <Typography
          variant="h4"
          fontWeight={800}
          color={emphasis ? color : colors.textPrimary}
          sx={{ fontVariantNumeric: "tabular-nums" }}
        >
          {value}
        </Typography>
        {sub && (
          <Typography variant="caption" color="text.secondary">
            {sub}
          </Typography>
        )}
      </CardContent>
    </Card>
  );
}

export interface BarSegment {
  value: number;
  color: string;
  label: string;
}

/**
 * Horizontal stacked magnitude bar. Rounded data-ends, a 2px surface gap between fills.
 * Pass `scaleMax` to render several bars on a shared scale (comparable lengths).
 */
export function StackedBar({
  segments,
  scaleMax,
  height = 40,
}: {
  segments: BarSegment[];
  scaleMax?: number;
  height?: number;
}) {
  const grown = useMounted();
  const total = segments.reduce((s, x) => s + x.value, 0);
  const denom = scaleMax ?? total;
  return (
    <Box
      sx={{
        display: "flex",
        gap: "2px",
        height,
        width: "100%",
        bgcolor: alpha(colors.textMuted, 0.12),
        borderRadius: 1.5,
        overflow: "hidden",
      }}
    >
      {segments.map((seg, i) => {
        const pct = denom ? (seg.value / denom) * 100 : 0;
        if (pct <= 0) return null;
        return (
          <Tooltip key={i} title={`${seg.label}`} arrow>
            <Box
              sx={{
                flexBasis: grown ? `${pct}%` : "0%",
                bgcolor: seg.color,
                borderRadius: 1,
                minWidth: grown ? 3 : 0,
                transition: "flex-basis 0.7s cubic-bezier(0.22, 1, 0.36, 1)",
              }}
            />
          </Tooltip>
        );
      })}
    </Box>
  );
}

/** A color dot + label + value used as a compact legend line under bars. */
export function LegendItem({
  color,
  label,
  value,
}: {
  color: string;
  label: string;
  value: string;
}) {
  return (
    <Box display="flex" alignItems="center" gap={1}>
      <Box sx={{ width: 10, height: 10, borderRadius: "3px", bgcolor: color, flexShrink: 0 }} />
      <Typography variant="body2" color="text.secondary">
        {label}
      </Typography>
      <Typography
        variant="body2"
        fontWeight={700}
        color={colors.textPrimary}
        sx={{ fontVariantNumeric: "tabular-nums" }}
      >
        {value}
      </Typography>
    </Box>
  );
}
