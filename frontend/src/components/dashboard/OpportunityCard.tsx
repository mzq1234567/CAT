import { Box, Button, Card, CardContent, Chip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import DnsOutlinedIcon from "@mui/icons-material/DnsOutlined";
import { colors } from "../../theme";
import type { Finding } from "../../types";
import { areaForCategory } from "./area";
import { AREA_ICON } from "./areaIcons";
import { affectedResources, affectedLabel, metaFor } from "./categoryMeta";
import { AreaTag, ImpactChip } from "./badges";
import { AREA_ACCENT, SAVINGS_COLOR, fmtCompact, fmtUSD } from "./tokens";

function clamp(lines: number) {
  return { display: "-webkit-box", WebkitLineClamp: lines, WebkitBoxOrient: "vertical" as const, overflow: "hidden" };
}

export default function OpportunityCard({
  finding,
  variant,
  onViewDetails,
}: {
  finding: Finding;
  variant: "featured" | "compact";
  onViewDetails: () => void;
}) {
  const area = areaForCategory(finding.category);
  const accent = AREA_ACCENT[area];
  const meta = metaFor(finding.category);
  const summary = meta.summary(Math.max(affectedResources(finding).count, 1));

  const AffectedChip = (
    <Chip
      size="small"
      variant="outlined"
      icon={<DnsOutlinedIcon sx={{ fontSize: 14 }} />}
      label={affectedLabel(finding)}
      sx={{ color: colors.textSecondary, borderColor: colors.border, fontWeight: 600, "& .MuiChip-icon": { color: colors.textMuted } }}
    />
  );

  // ── Featured: a large, prominent card for the biggest opportunities ────────
  if (variant === "featured") {
    return (
      <Card
        sx={{
          position: "relative", overflow: "hidden", height: "100%",
          borderColor: alpha(accent, 0.32),
          background: `linear-gradient(160deg, ${alpha(accent, 0.09)} 0%, ${alpha(colors.surface, 0)} 55%)`,
          transition: "box-shadow .2s ease, border-color .2s ease",
          "&:hover": { boxShadow: `0 12px 34px ${alpha(accent, 0.16)}`, borderColor: alpha(accent, 0.5) },
        }}
      >
        <Box sx={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 4, bgcolor: accent }} />
        <CardContent sx={{ p: 3, pl: 3.25, height: "100%", display: "flex", flexDirection: "column" }}>
          <Box display="flex" alignItems="center" gap={1} mb={1.5} flexWrap="wrap">
            <Box sx={{ width: 34, height: 34, borderRadius: 1.75, display: "flex", alignItems: "center", justifyContent: "center", bgcolor: alpha(accent, 0.14), color: accent, mr: 0.25 }}>
              {AREA_ICON[area]}
            </Box>
            <AreaTag area={area} />
            <ImpactChip severity={finding.severity} />
          </Box>

          <Typography variant="h6" fontWeight={800} color={colors.textPrimary} mb={0.75}>
            {finding.display_name}
          </Typography>
          <Typography variant="body2" color="text.secondary" mb={2.5} sx={clamp(2)}>
            {summary}
          </Typography>

          <Box display="flex" alignItems="flex-end" justifyContent="space-between" gap={2} mt="auto">
            <Box>
              <Typography fontWeight={800} color={SAVINGS_COLOR} sx={{ fontSize: "2rem", lineHeight: 1, fontVariantNumeric: "tabular-nums" }}>
                {fmtCompact(finding.estimated_savings_annual)}
              </Typography>
              <Typography variant="caption" color={colors.textMuted}>
                per year · {fmtUSD(finding.estimated_savings_monthly)} / mo
              </Typography>
            </Box>
            <Button variant="contained" onClick={onViewDetails} endIcon={<ArrowForwardIcon />} sx={{ flexShrink: 0 }}>
              View details
            </Button>
          </Box>

          <Box mt={2} pt={1.75} sx={{ borderTop: `1px solid ${colors.border}` }}>{AffectedChip}</Box>
        </CardContent>
      </Card>
    );
  }

  // ── Compact: a denser card for smaller findings ────────────────────────────
  return (
    <Card
      sx={{
        position: "relative", overflow: "hidden", height: "100%",
        transition: "box-shadow .2s ease, border-color .2s ease",
        "&:hover": { boxShadow: `0 6px 20px ${alpha(accent, 0.1)}`, borderColor: alpha(accent, 0.42) },
      }}
    >
      <Box sx={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 3, bgcolor: accent }} />
      <CardContent sx={{ p: 2.25, pl: 2.6, height: "100%", display: "flex", flexDirection: "column" }}>
        <Box display="flex" gap={0.75} mb={1} flexWrap="wrap">
          <AreaTag area={area} />
          <ImpactChip severity={finding.severity} />
        </Box>
        <Typography variant="subtitle1" fontWeight={700} color={colors.textPrimary} sx={{ ...clamp(1) }}>
          {finding.display_name}
        </Typography>
        <Typography variant="body2" color="text.secondary" mt={0.25} mb={1.75} sx={clamp(2)}>
          {summary}
        </Typography>

        <Box display="flex" alignItems="flex-end" justifyContent="space-between" gap={1} mt="auto">
          <Box>
            <Typography fontWeight={800} color={SAVINGS_COLOR} sx={{ fontSize: "1.35rem", lineHeight: 1, fontVariantNumeric: "tabular-nums" }}>
              {fmtCompact(finding.estimated_savings_annual)}
            </Typography>
            <Typography variant="caption" color={colors.textMuted}>
              per year
            </Typography>
          </Box>
          <Button size="small" onClick={onViewDetails} endIcon={<ArrowForwardIcon sx={{ fontSize: 16 }} />} sx={{ textTransform: "none", color: colors.accentBlue, flexShrink: 0 }}>
            View details
          </Button>
        </Box>
      </CardContent>
    </Card>
  );
}
