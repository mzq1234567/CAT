import { Box, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { colors } from "../../../theme";
import { useMounted } from "./useMounted";

/**
 * A three-step spend waterfall: Current Spend − Savings = Projected Spend.
 * The middle "Savings" bar floats between the projected top and the current top, so the eye reads
 * the reduction as a physical drop. Only meaningful when savings ≤ current spend (the caller guards).
 */
export function Waterfall({
  current,
  savings,
  projected,
  format,
}: {
  current: number;
  savings: number;
  projected: number;
  format: (n: number) => string;
}) {
  const grown = useMounted();
  const max = Math.max(current, 1);
  const h = (v: number) => `${(v / max) * 100}%`;

  const Col = ({
    label,
    value,
    children,
  }: {
    label: string;
    value: string;
    children: React.ReactNode;
  }) => (
    <Box flex={1} display="flex" flexDirection="column" alignItems="center" minWidth={0}>
      <Typography variant="caption" fontWeight={700} color={colors.textPrimary} sx={{ fontVariantNumeric: "tabular-nums" }} noWrap>
        {value}
      </Typography>
      <Box position="relative" width="100%" height={150} mt={0.5} display="flex" alignItems="flex-end" justifyContent="center">
        {children}
      </Box>
      <Typography variant="caption" color={colors.textMuted} mt={0.75} textAlign="center" sx={{ lineHeight: 1.2 }}>
        {label}
      </Typography>
    </Box>
  );

  const bar = (height: string, color: string) => ({
    width: "62%",
    maxWidth: 64,
    height: grown ? height : "0%",
    borderRadius: "5px 5px 0 0",
    background: `linear-gradient(180deg, ${color}, ${alpha(color, 0.82)})`,
    transition: "height 0.7s cubic-bezier(0.22,1,0.36,1)",
  });

  return (
    <Box display="flex" alignItems="flex-end" gap={1.5} height={210}>
      {/* Current spend — full bar */}
      <Col label="Current spend" value={format(current)}>
        <Box sx={bar(h(current), "#3B82F6")} />
      </Col>

      {/* Savings — floats from the projected top up to the current top */}
      <Col label="Savings" value={`− ${format(savings)}`}>
        <Box
          sx={{
            width: "62%",
            maxWidth: 64,
            position: "absolute",
            bottom: h(projected),
            height: grown ? h(savings) : "0%",
            borderRadius: "5px",
            background: `linear-gradient(180deg, ${colors.success}, ${alpha(colors.success, 0.8)})`,
            transition: "height 0.7s cubic-bezier(0.22,1,0.36,1)",
          }}
        />
      </Col>

      {/* Projected spend */}
      <Col label="Projected spend" value={format(projected)}>
        <Box sx={bar(h(projected), colors.accentBlue)} />
      </Col>
    </Box>
  );
}
