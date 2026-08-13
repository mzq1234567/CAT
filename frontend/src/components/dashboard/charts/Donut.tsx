import { Box, Typography } from "@mui/material";
import { Cell, Pie, PieChart, ResponsiveContainer } from "recharts";
import { colors } from "../../../theme";

export interface DonutDatum {
  name: string;
  value: number;
  color: string;
}

/**
 * Informational donut — HOVERABLE, never CLICKABLE.
 *
 * Emphasis on hover is done purely by DIMMING the other slices (fill-opacity), so the geometry never
 * changes: every segment reads identically at every proportion (no `activeShape`, no radius pop, so a
 * small slice can't distort and a dominant slice can't wobble). The hovered segment's name/value/share
 * surface in the empty centre. There is no click handler, no selection, cursor is `default`, and all
 * SVG focus outlines are suppressed — so clicking a segment does nothing and leaves no black border.
 */
export function Donut({
  data,
  total,
  centerValue,
  centerLabel,
  activeIndex,
  onActive,
  format,
  size = 172,
}: {
  data: DonutDatum[];
  total: number;
  centerValue: string;
  centerLabel: string;
  activeIndex: number | null;
  onActive: (i: number | null) => void;
  format: (n: number) => string;
  size?: number;
}) {
  const active = activeIndex != null ? data[activeIndex] : null;
  const sharePct = active && total ? Math.round((active.value / total) * 100) : 0;

  return (
    <Box
      sx={{
        position: "relative",
        width: size,
        height: size,
        flexShrink: 0,
        // The donut is not a control: default cursor, and no focus ring / outline on any SVG node so a
        // click can never leave a black border or selection artefact.
        cursor: "default",
        "& svg, & path, & g, & .recharts-sector, & .recharts-wrapper, & *:focus, & *:focus-visible": {
          outline: "none !important",
        },
        "& path": { cursor: "default" },
      }}
    >
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={data}
            dataKey="value"
            nameKey="name"
            innerRadius={size * 0.34}
            outerRadius={size * 0.47}
            paddingAngle={data.length > 1 ? 2 : 0}
            stroke={colors.surface}
            strokeWidth={2}
            isAnimationActive={false}
            // No onClick, no activeIndex/activeShape → geometry is constant; hover only dims the rest.
            onMouseEnter={(_: unknown, i: number) => onActive(i)}
            onMouseLeave={() => onActive(null)}
          >
            {data.map((d, i) => (
              <Cell
                key={i}
                fill={d.color}
                fillOpacity={activeIndex == null || activeIndex === i ? 1 : 0.32}
                style={{ transition: "fill-opacity .18s ease", outline: "none", cursor: "default" }}
                tabIndex={-1}
              />
            ))}
          </Pie>
        </PieChart>
      </ResponsiveContainer>

      <Box
        sx={{
          position: "absolute",
          inset: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          textAlign: "center",
          pointerEvents: "none",
          px: 2,
        }}
      >
        {active ? (
          <>
            <Box display="flex" alignItems="center" gap={0.75}>
              <Box sx={{ width: 9, height: 9, borderRadius: "2px", bgcolor: active.color }} />
              <Typography variant="body2" fontWeight={700} color={colors.textPrimary} noWrap>
                {active.name}
              </Typography>
            </Box>
            <Typography variant="h6" fontWeight={800} sx={{ color: active.color, lineHeight: 1.1 }}>
              {format(active.value)}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {sharePct}% of savings
            </Typography>
          </>
        ) : (
          <>
            <Typography variant="h5" fontWeight={800} color={colors.textPrimary} sx={{ lineHeight: 1 }}>
              {centerValue}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {centerLabel}
            </Typography>
          </>
        )}
      </Box>
    </Box>
  );
}
