import { Box, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { colors } from "../../../theme";
import { useMounted } from "./useMounted";

/**
 * Horizontal utilisation meter with an optional threshold marker.
 * Used to make a finding's *evidence* visible — e.g. "peak CPU 15% vs a 70% downsize ceiling".
 */
export function Meter({
  label,
  value,
  max = 100,
  unit = "%",
  color,
  threshold,
  thresholdLabel,
  unavailable = false,
}: {
  label: string;
  value: number | null;
  max?: number;
  unit?: string;
  color: string;
  threshold?: number;
  thresholdLabel?: string;
  unavailable?: boolean;
}) {
  const grown = useMounted();
  const pct = value == null ? 0 : Math.max(0, Math.min(100, (value / max) * 100));
  const threshPct = threshold == null ? null : Math.max(0, Math.min(100, (threshold / max) * 100));

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="baseline" mb={0.75}>
        <Typography variant="caption" color="text.secondary" fontWeight={600}>
          {label}
        </Typography>
        {unavailable || value == null ? (
          <Typography variant="caption" color={colors.textMuted} fontStyle="italic">
            not measured
          </Typography>
        ) : (
          <Typography variant="body2" fontWeight={800} sx={{ color, fontVariantNumeric: "tabular-nums" }}>
            {value}
            {unit}
          </Typography>
        )}
      </Box>

      <Box
        sx={{
          position: "relative",
          height: 12,
          borderRadius: 6,
          bgcolor: unavailable ? alpha(colors.textMuted, 0.12) : alpha(color, 0.12),
          overflow: "hidden",
        }}
      >
        {!unavailable && value != null && (
          <Box
            sx={{
              position: "absolute",
              inset: 0,
              width: grown ? `${pct}%` : "0%",
              borderRadius: 6,
              background: `linear-gradient(90deg, ${alpha(color, 0.85)}, ${color})`,
              transition: "width 0.8s cubic-bezier(0.22, 1, 0.36, 1)",
            }}
          />
        )}
        {threshPct != null && (
          <Box
            sx={{
              position: "absolute",
              top: -2,
              bottom: -2,
              left: `${threshPct}%`,
              width: "2px",
              bgcolor: colors.textPrimary,
              opacity: 0.55,
            }}
          />
        )}
      </Box>

      {threshold != null && thresholdLabel && (
        <Box sx={{ position: "relative", height: 14, mt: 0.25 }}>
          <Typography
            variant="caption"
            color={colors.textMuted}
            sx={{
              position: "absolute",
              left: `${threshPct}%`,
              transform: "translateX(-50%)",
              whiteSpace: "nowrap",
              fontSize: 10,
            }}
          >
            {thresholdLabel}
          </Typography>
        </Box>
      )}
    </Box>
  );
}
