import { Box, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { colors } from "../../../theme";
import { useMounted } from "./useMounted";

export interface HBar {
  label: string;
  value: number;
  color: string;
}

/** Compact horizontal bar list (e.g. findings by impact), scaled to the largest value. */
export function HBars({ data, valueSuffix = "" }: { data: HBar[]; valueSuffix?: string }) {
  const grown = useMounted();
  const max = Math.max(...data.map((d) => d.value), 1);
  return (
    <Box display="flex" flexDirection="column" gap={1.5}>
      {data.map((d) => (
        <Box key={d.label} sx={{ "&:hover .hbar-fill": { filter: "brightness(1.08)" } }}>
          <Box display="flex" justifyContent="space-between" alignItems="baseline" mb={0.5}>
            <Box display="flex" alignItems="center" gap={0.9}>
              <Box sx={{ width: 9, height: 9, borderRadius: "3px", bgcolor: d.color }} />
              <Typography variant="body2" color="text.secondary">
                {d.label}
              </Typography>
            </Box>
            <Typography variant="body2" fontWeight={700} color={colors.textPrimary} sx={{ fontVariantNumeric: "tabular-nums" }}>
              {d.value}
              {valueSuffix}
            </Typography>
          </Box>
          <Box sx={{ height: 8, borderRadius: 1, bgcolor: alpha(colors.textMuted, 0.12), overflow: "hidden" }}>
            <Box
              className="hbar-fill"
              sx={{
                height: "100%",
                width: grown ? `${(d.value / max) * 100}%` : "0%",
                minWidth: d.value > 0 && grown ? 4 : 0,
                borderRadius: 1,
                bgcolor: d.color,
                transition: "width 0.7s cubic-bezier(0.22, 1, 0.36, 1), filter 0.15s ease",
              }}
            />
          </Box>
        </Box>
      ))}
    </Box>
  );
}
