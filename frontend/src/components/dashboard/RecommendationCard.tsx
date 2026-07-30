import React from "react";
import { Box, Card, CardContent, Chip, Collapse, Grid, IconButton, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import KeyboardArrowDownIcon from "@mui/icons-material/KeyboardArrowDown";
import LightbulbOutlinedIcon from "@mui/icons-material/LightbulbOutlined";
import SettingsSuggestOutlinedIcon from "@mui/icons-material/SettingsSuggestOutlined";
import DnsOutlinedIcon from "@mui/icons-material/DnsOutlined";
import ReportProblemOutlinedIcon from "@mui/icons-material/ReportProblemOutlined";
import { colors } from "../../theme";
import type { Finding } from "../../types";
import { areaForCategory } from "./area";
import { AreaTag, ImpactChip, ConfidenceChip, ValidationChip } from "./badges";
import FindingEvidence from "./FindingEvidence";
import { SAVINGS_COLOR, fmtCompact, fmtUSD } from "./tokens";

function DetailBlock({
  icon,
  label,
  children,
  accent,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
  accent?: string;
}) {
  return (
    <Box
      sx={{
        height: "100%",
        p: 2,
        borderRadius: 2,
        bgcolor: colors.surfaceElevated,
        border: `1px solid ${colors.border}`,
      }}
    >
      <Box display="flex" alignItems="center" gap={0.75} mb={0.75} sx={{ color: accent ?? colors.textSecondary }}>
        {icon}
        <Typography variant="caption" fontWeight={700} textTransform="uppercase" letterSpacing="0.05em">
          {label}
        </Typography>
      </Box>
      <Typography variant="body2" color={colors.textPrimary}>
        {children}
      </Typography>
    </Box>
  );
}

export default function RecommendationCard({ finding }: { finding: Finding }) {
  const [open, setOpen] = React.useState(false);
  const area = areaForCategory(finding.category);
  const memoryUnverified = (finding.details as { memory_verified?: boolean } | null)?.memory_verified === false;

  return (
    <Card sx={{ "&:hover": { borderColor: alpha(colors.accentBlue, 0.4) } }}>
      <CardContent
        onClick={() => setOpen((o) => !o)}
        sx={{ p: 2.5, cursor: "pointer", "&:last-child": { pb: 2.5 } }}
      >
        <Box display="flex" alignItems="center" gap={2}>
          <Box flex={1} minWidth={0}>
            <Box display="flex" alignItems="center" gap={1} mb={0.75} flexWrap="wrap">
              <AreaTag area={area} />
              <ImpactChip severity={finding.severity} />
            </Box>
            <Typography variant="subtitle1" fontWeight={700} color={colors.textPrimary} noWrap>
              {finding.display_name}
            </Typography>
            <Typography variant="body2" color="text.secondary" noWrap>
              {finding.resource_name || finding.resource_group || "Multiple resources"}
            </Typography>
          </Box>

          <Box textAlign="right" sx={{ flexShrink: 0 }}>
            <Typography
              variant="h6"
              fontWeight={800}
              color={SAVINGS_COLOR}
              sx={{ fontVariantNumeric: "tabular-nums", lineHeight: 1.1 }}
            >
              {fmtCompact(finding.estimated_savings_annual)}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              per year
            </Typography>
          </Box>
          <IconButton size="small" sx={{ color: colors.textSecondary, flexShrink: 0 }}>
            <KeyboardArrowDownIcon
              sx={{ transform: open ? "rotate(180deg)" : "none", transition: "transform 0.2s ease" }}
            />
          </IconButton>
        </Box>
      </CardContent>

      <Collapse in={open} timeout="auto" unmountOnExit>
        <Box sx={{ px: 2.5, pb: 2.5 }}>
          {/* Why — the finding's real, plain-language explanation */}
          <Box
            sx={{
              p: 2,
              mb: 2,
              borderRadius: 2,
              bgcolor: alpha(colors.warning, 0.06),
              border: `1px solid ${alpha(colors.warning, 0.25)}`,
            }}
          >
            <Box display="flex" alignItems="center" gap={0.75} mb={0.5} sx={{ color: colors.warning }}>
              <LightbulbOutlinedIcon fontSize="small" />
              <Typography variant="caption" fontWeight={700} textTransform="uppercase" letterSpacing="0.05em">
                Why this recommendation exists
              </Typography>
            </Box>
            <Typography variant="body2" color={colors.textPrimary}>
              {finding.description || "—"}
            </Typography>
          </Box>

          {/* Evidence — charts that show WHY this finding fired (utilisation, resize, cost) */}
          <FindingEvidence finding={finding} />

          <Grid container spacing={2}>
            <Grid item xs={12} md={6}>
              <DetailBlock
                icon={<SettingsSuggestOutlinedIcon fontSize="small" />}
                label="Recommended action"
                accent={colors.accentBlue}
              >
                {finding.recommendation || "—"}
              </DetailBlock>
            </Grid>
            <Grid item xs={12} md={6}>
              <DetailBlock icon={<DnsOutlinedIcon fontSize="small" />} label="Resource">
                {[finding.resource_name, finding.resource_type, finding.resource_group]
                  .filter(Boolean)
                  .join(" · ") || "—"}
              </DetailBlock>
            </Grid>
            <Grid item xs={12}>
              <Box
                sx={{
                  p: 2,
                  borderRadius: 2,
                  bgcolor: alpha(SAVINGS_COLOR, 0.08),
                  border: `1px solid ${alpha(SAVINGS_COLOR, 0.25)}`,
                  display: "flex",
                  alignItems: "baseline",
                  gap: 2,
                  flexWrap: "wrap",
                }}
              >
                <Typography variant="caption" fontWeight={700} textTransform="uppercase" letterSpacing="0.05em" color="text.secondary">
                  Estimated savings
                </Typography>
                <Typography variant="h6" fontWeight={800} color={SAVINGS_COLOR} sx={{ fontVariantNumeric: "tabular-nums" }}>
                  {fmtUSD(finding.estimated_savings_annual)} / yr
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  {fmtUSD(finding.estimated_savings_monthly)} / mo
                </Typography>
                {finding.actual_monthly_cost != null && (
                  <Typography variant="body2" color="text.secondary">
                    · actual resource cost {fmtUSD(finding.actual_monthly_cost)} / mo
                  </Typography>
                )}
              </Box>
            </Grid>
          </Grid>

          <Box display="flex" gap={1} mt={2} flexWrap="wrap">
            <ConfidenceChip confidence={finding.confidence} />
            <ValidationChip finding={finding} />
            {memoryUnverified && (
              <Chip
                size="small"
                variant="outlined"
                icon={<ReportProblemOutlinedIcon sx={{ fontSize: 15 }} />}
                label="Memory not verified"
                sx={{ color: colors.warning, borderColor: alpha(colors.warning, 0.4) }}
              />
            )}
            {finding.advisor_recommendation_id && (
              <Chip
                size="small"
                variant="outlined"
                label="Confirmed by Azure Advisor"
                sx={{ color: colors.textSecondary, borderColor: colors.border }}
              />
            )}
          </Box>
        </Box>
      </Collapse>
    </Card>
  );
}
