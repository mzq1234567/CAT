import { Box, Typography } from "@mui/material";
import { Cell, Pie, PieChart, ResponsiveContainer, Sector } from "recharts";
import { colors } from "../../../theme";

export interface DonutDatum {
  name: string;
  value: number;
  color: string;
}

/** The hovered slice pops out slightly — the interaction cue, no floating tooltip needed.
 *  A near-full slice (e.g. one area at 99%) is NOT expanded: growing an almost-complete ring
 *  just makes the whole donut lurch/wobble. For dominant slices we brighten with a subtle inner
 *  ring instead, so the hover cue is clean at every proportion. */
function ActiveSlice(props: any) {
  const { cx, cy, innerRadius, outerRadius, startAngle, endAngle, fill } = props;
  const dominant = Math.abs(endAngle - startAngle) > 200; // > ~55% of the circle
  return (
    <Sector
      cx={cx}
      cy={cy}
      innerRadius={dominant ? innerRadius - 3 : innerRadius}
      outerRadius={dominant ? outerRadius : outerRadius + 7}
      startAngle={startAngle}
      endAngle={endAngle}
      fill={fill}
      cornerRadius={3}
    />
  );
}

/**
 * Interactive donut: hovering a slice (or its legend row) pops the slice and swaps the CENTER
 * label to that segment's name + value + share — info lives in the empty center, so nothing
 * overlaps. `activeIndex`/`onActive` are controlled by the parent so the legend stays in sync.
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
    <Box sx={{ position: "relative", width: size, height: size, flexShrink: 0 }}>
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
            activeIndex={activeIndex ?? undefined}
            activeShape={ActiveSlice}
            onMouseEnter={(_: any, i: number) => onActive(i)}
            onMouseLeave={() => onActive(null)}
            animationDuration={650}
          >
            {data.map((d, i) => (
              <Cell key={i} fill={d.color} cursor="pointer" />
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
