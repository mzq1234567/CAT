import { Box, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import ArrowRightAltIcon from "@mui/icons-material/ArrowRightAlt";
import { colors } from "../../../theme";
import { SAVINGS_COLOR, SPEND_COLOR } from "../tokens";

/**
 * Before → after comparison for a single dimension (vCPU, RAM, cost) on a downsize.
 * Two proportional bars (current vs recommended) sharing a scale, so the reduction is visible.
 */
export function CompareRow({
  label,
  before,
  after,
  format,
}: {
  label: string;
  before: number;
  after: number;
  format: (v: number) => string;
}) {
  const max = Math.max(before, after, 1);
  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="baseline" mb={0.75}>
        <Typography variant="caption" color="text.secondary" fontWeight={600}>
          {label}
        </Typography>
        <Box display="flex" alignItems="center" gap={0.75}>
          <Typography variant="body2" color={colors.textMuted} sx={{ fontVariantNumeric: "tabular-nums" }}>
            {format(before)}
          </Typography>
          <ArrowRightAltIcon sx={{ fontSize: 16, color: colors.textMuted }} />
          <Typography variant="body2" fontWeight={800} sx={{ color: SAVINGS_COLOR, fontVariantNumeric: "tabular-nums" }}>
            {format(after)}
          </Typography>
        </Box>
      </Box>
      <Box display="flex" flexDirection="column" gap={0.5}>
        <Bar value={before} max={max} color={SPEND_COLOR} />
        <Bar value={after} max={max} color={SAVINGS_COLOR} />
      </Box>
    </Box>
  );
}

function Bar({ value, max, color }: { value: number; max: number; color: string }) {
  return (
    <Box sx={{ height: 7, borderRadius: 1, bgcolor: alpha(colors.textMuted, 0.1), overflow: "hidden" }}>
      <Box
        sx={{
          height: "100%",
          width: `${(value / max) * 100}%`,
          minWidth: 3,
          borderRadius: 1,
          bgcolor: color,
          transition: "width 0.5s ease",
        }}
      />
    </Box>
  );
}
