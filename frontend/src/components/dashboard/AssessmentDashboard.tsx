import React from "react";
import { Alert, Box, Chip, Tooltip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import ReportProblemOutlinedIcon from "@mui/icons-material/ReportProblemOutlined";
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import { colors } from "../../theme";
import type { Assessment } from "../../types";
import { Area, areaForCategory, realisableFindings, countedAnnual } from "./area";
import { SectionHeader } from "./primitives";
import ExecutiveSummary from "./ExecutiveSummary";
import RecommendationInsights from "./RecommendationInsights";
import OptimizationCategories from "./OptimizationCategories";
import OpportunityGrid from "./OpportunityGrid";
import { setReportCurrency } from "./tokens";

export default function AssessmentDashboard({ assessment }: { assessment: Assessment }) {
  // Dismissed findings drop out of every view; the backend re-rolls the headline totals on dismiss.
  const findings = assessment.findings.filter((f) => !f.dismissed);
  const [area, setArea] = React.useState<Area | null>(null);

  // Render every figure in the subscription's billing currency (detected from Cost Management).
  setReportCurrency(assessment.currency);

  // Realisable annual savings (conditional AHB excluded) — computed from live findings so every view
  // stays consistent and reflects exclusions immediately, independent of the stored backend total.
  const totalSavings = React.useMemo(
    () => realisableFindings(findings).reduce((s, f) => s + countedAnnual(f), 0),
    [findings]
  );
  const excluded = React.useMemo(
    () => assessment.findings.filter((f) => f.dismissed),
    [assessment.findings]
  );

  // When a category is selected, show that area's realisable findings (conditional AHB isn't a
  // category); the unfiltered "All" view still lists every opportunity, AHB included.
  const filtered = React.useMemo(
    () => (area ? realisableFindings(findings).filter((f) => areaForCategory(f.category) === area) : findings),
    [findings, area]
  );

  // Degraded-run banner — Cost Management returned no per-resource billed cost, so grounded findings were
  // withheld rather than estimated from list price. Rendered at the top of whichever branch runs below.
  const degradedBanner = assessment.billing_detail_unavailable ? (
    <Box
      display="flex"
      alignItems="flex-start"
      gap={1.25}
      mb={3}
      p={2}
      sx={{
        borderRadius: 2,
        bgcolor: alpha(colors.warning, 0.1),
        border: `1px solid ${alpha(colors.warning, 0.35)}`,
      }}
    >
      <ReportProblemOutlinedIcon sx={{ fontSize: 20, color: colors.warning, mt: "1px", flexShrink: 0 }} />
      <Box>
        <Typography variant="body2" fontWeight={700} color={colors.textPrimary}>
          Billing detail was unavailable for this run — results are incomplete.
        </Typography>
        <Typography variant="caption" color={colors.textSecondary} sx={{ lineHeight: 1.6 }}>
          Azure Cost Management returned the subscription total but not per‑resource billed cost
          (usually a temporary throttle). Grounded findings — right‑sizing, Azure Hybrid Benefit and
          idle‑resource savings — were <b>withheld</b> rather than estimated from list price, so what you
          see below is a subset. Re‑run in a few minutes for accurate, grounded figures.
        </Typography>
      </Box>
    </Box>
  ) : null;

  // Data-collection banner — some Azure data could not be collected (throttle/error), so the run is
  // PARTIAL/FAILED. Shown for non-billing causes (the billing-specific case has its own banner above).
  const collectionIncomplete = assessment.data_quality !== "complete";
  const dataQualityBanner =
    collectionIncomplete && !assessment.billing_detail_unavailable && assessment.data_quality_message ? (
      <Box
        display="flex"
        alignItems="flex-start"
        gap={1.25}
        mb={3}
        p={2}
        sx={{
          borderRadius: 2,
          bgcolor: alpha(colors.warning, 0.1),
          border: `1px solid ${alpha(colors.warning, 0.35)}`,
        }}
      >
        <ReportProblemOutlinedIcon sx={{ fontSize: 20, color: colors.warning, mt: "1px", flexShrink: 0 }} />
        <Box>
          <Typography variant="body2" fontWeight={700} color={colors.textPrimary}>
            {assessment.data_quality === "failed"
              ? "Azure data could not be collected for this assessment."
              : "Some Azure data could not be collected — results are incomplete."}
          </Typography>
          <Typography variant="caption" color={colors.textSecondary} sx={{ lineHeight: 1.6 }}>
            {assessment.data_quality_message}
          </Typography>
        </Box>
      </Box>
    ) : null;

  // Honest, always-present assessment-state indicator. "Complete" is stated only when we actually
  // collected every data source — it's earned, not the mere absence of a warning; PARTIAL/FAILED runs
  // say so plainly (the banners above carry the detail).
  const partialRun = collectionIncomplete || assessment.billing_detail_unavailable;
  const failedRun = assessment.data_quality === "failed";
  const statusMeta = failedRun
    ? {
        color: colors.error,
        Icon: ReportProblemOutlinedIcon,
        label: "Incomplete assessment",
        tip: "Azure data could not be collected for this run, so this is not a complete assessment. Re-run once access/throttling clears.",
      }
    : partialRun
    ? {
        color: colors.warning,
        Icon: ReportProblemOutlinedIcon,
        label: "Partial data collected",
        tip: "Some Azure data couldn't be collected this run, so results are a subset. Missing data is never treated as zero — affected findings were withheld or shown as 'not quantified'. Re-run for complete figures.",
      }
    : {
        color: colors.success,
        Icon: CheckCircleOutlineIcon,
        label: "Complete — all data sources collected",
        tip: "Every data source the assessment relies on (inventory, metrics, billing, pricing) was collected successfully for this run.",
      };
  const statusChip = (
    <Tooltip title={statusMeta.tip} arrow placement="top">
      <Chip
        icon={<statusMeta.Icon sx={{ fontSize: 16 }} />}
        label={statusMeta.label}
        size="small"
        sx={{
          mb: 2.5,
          cursor: "help",
          fontWeight: 700,
          bgcolor: alpha(statusMeta.color, 0.12),
          color: statusMeta.color,
          border: `1px solid ${alpha(statusMeta.color, 0.3)}`,
          "& .MuiChip-icon": { color: statusMeta.color },
        }}
      />
    </Tooltip>
  );

  if (findings.length === 0) {
    return (
      <>
        {degradedBanner}
        {dataQualityBanner}
        {statusChip}
        <ExecutiveSummary assessment={assessment} />
        {/* Only claim "well-optimized" when we actually had the data to judge — not when a throttle or a
            failed collection withheld everything (covered by the banners above). */}
        {!assessment.billing_detail_unavailable && !collectionIncomplete && (
          <Alert
            severity="success"
            sx={{ mt: 3, bgcolor: alpha(colors.success, 0.1), border: `1px solid ${alpha(colors.success, 0.3)}` }}
          >
            No cost optimization findings detected — this environment looks well-optimized.
          </Alert>
        )}
      </>
    );
  }

  return (
    <Box>
      {degradedBanner}
      {dataQualityBanner}
      {statusChip}

      {/* 01 — the financial headline: three KPI cards (the 5-second story), the focus of the page */}
      <Box mb={3}>
        <ExecutiveSummary assessment={assessment} />
      </Box>

      {/* Scan coverage — quiet metadata caption, not a prominent band */}
      {assessment.total_resources != null && assessment.total_resources > 0 && (
        <Typography
          variant="caption"
          color={colors.textMuted}
          sx={{ display: "block", mt: -0.5, mb: 3.5, letterSpacing: "0.01em" }}
        >
          {assessment.total_resources.toLocaleString()} resources scanned
          {" · "}{assessment.resource_type_count} resource types
          {" · "}{findings.length} optimization {findings.length === 1 ? "opportunity" : "opportunities"} identified
        </Typography>
      )}

      {/* 02 — the Recommendations experience */}
      <Box>
        <SectionHeader
          title="Optimization Opportunities"
          subtitle="Where the savings are, the biggest wins, and every recommendation — open any one for the full breakdown."
        />

        {/* Executive visualizations — break up the page with charts, not just cards */}
        <Box mb={4}>
          <RecommendationInsights findings={findings} assessment={assessment} />
        </Box>

        {/* Level 1 — optimization categories are the primary navigation / filter */}
        <Box mb={4}>
          <OptimizationCategories
            findings={findings}
            totalAnnual={totalSavings}
            selected={area}
            onSelect={setArea}
          />
        </Box>

        {/* Level 2 / 3 — featured + compact opportunity cards (filtered by the selected category) */}
        <OpportunityGrid findings={filtered} excludedFindings={excluded} assessmentId={assessment.id} />
      </Box>
    </Box>
  );
}
