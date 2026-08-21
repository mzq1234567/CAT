import React from "react";
import { Alert, Box, Chip, Tooltip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import ReportProblemOutlinedIcon from "@mui/icons-material/ReportProblemOutlined";
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import HourglassEmptyIcon from "@mui/icons-material/HourglassEmpty";
import CloudOffIcon from "@mui/icons-material/CloudOff";
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
          Billing detail was unavailable for this run, results are incomplete.
        </Typography>
        <Typography variant="caption" color={colors.textSecondary} sx={{ lineHeight: 1.6 }}>
          Azure Cost Management returned the subscription total but not per‑resource billed cost
          (usually a temporary throttle). Grounded findings, right‑sizing, Azure Hybrid Benefit and
          idle‑resource savings were <b>withheld</b> rather than estimated from list price, so what you
          see below is a subset. Re‑run in a few minutes for accurate, grounded figures.
        </Typography>
      </Box>
    </Box>
  ) : null;

  // Data-collection banner — some Azure data could not be collected (throttle/error), so the run is
  // PARTIAL/FAILED. Shown for non-billing causes (the billing-specific case has its own banner above).
  const collectionIncomplete = assessment.data_quality !== "complete";
  // A PARTIAL run whose only missing piece is billing (Cost Management was throttled this run) — distinct
  // from a genuinely-new subscription (which collects billing fine but has no history: data_quality stays
  // "complete"). Resource-based findings still work, so we DON'T call the whole assessment incomplete here.
  const billingUnavailable = !assessment.cost_data_available && assessment.data_quality === "partial";
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
              : billingUnavailable
              ? "Billing data temporarily unavailable."
              : "Some Azure data could not be collected. Results are incomplete."}
          </Typography>
          <Typography variant="caption" color={colors.textSecondary} sx={{ lineHeight: 1.6 }}>
            {billingUnavailable
              ? "Azure billing data could not be retrieved for this run because the Cost Management API was throttled. Resource-based findings are still available, but cost-based savings could not be quantified. Re-run shortly."
              : assessment.data_quality_message}
          </Typography>
        </Box>
      </Box>
    ) : null;

  // Honest, always-present assessment-state indicator. "Complete" is stated only when we actually
  // collected every data source — it's earned, not the mere absence of a warning; PARTIAL/FAILED runs
  // say so plainly (the banners above carry the detail).
  const partialRun = collectionIncomplete || assessment.billing_detail_unavailable;
  const failedRun = assessment.data_quality === "failed";
  // A new or recently-migrated subscription: resources were collected fine, but Azure Cost Management
  // has no billing history yet, so cost-dependent savings can't be quantified. This is NOT a failure and
  // NOT "fully optimized", so it gets its own honest, neutral state rather than "Complete".
  const awaitingBilling = !assessment.cost_data_available && !failedRun && !partialRun;
  const statusMeta = failedRun
    ? {
        color: colors.error,
        Icon: ReportProblemOutlinedIcon,
        label: "Incomplete assessment",
        tip: "Azure data could not be collected for this run, so this is not a complete assessment. Re-run once access/throttling clears.",
      }
    : billingUnavailable
    ? {
        color: colors.warning,
        Icon: CloudOffIcon,
        label: "Billing data temporarily unavailable",
        tip: "Resource inventory and the other data sources were collected successfully, but the Azure Cost Management (billing) API was throttled this run, so cost-based savings couldn't be quantified. Resource-based findings are still shown; this is temporary, re-run shortly for cost figures.",
      }
    : partialRun
    ? {
        color: colors.warning,
        Icon: ReportProblemOutlinedIcon,
        label: "Partial data collected",
        tip: "Some Azure data couldn't be collected this run, so results are a subset. Missing data is never treated as zero, so affected findings were withheld or shown as 'not quantified'. Re-run for complete figures.",
      }
    : awaitingBilling
    ? {
        color: colors.accentBlue,
        Icon: HourglassEmptyIcon,
        label: "Awaiting billing data",
        tip: "Current resources were collected successfully, but Azure Cost Management has no billing history for this subscription yet (common for a new or recently migrated subscription). Savings that depend on billed cost can't be quantified until that history accrues, so ₹0 here means 'not yet quantifiable', not 'no potential'. Re-run once billing data is available.",
      }
    : {
        color: colors.success,
        Icon: CheckCircleOutlineIcon,
        label: "Complete: all data sources collected",
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
        {/* Speak only to what we actually evaluated: the resources currently in this subscription,
            checked against the available recommendations. We stop short of declaring the whole
            environment "optimized". Still gated on having had complete data to judge — a throttle or a
            failed collection withholds everything (covered by the banners above). */}
        {!assessment.billing_detail_unavailable && !collectionIncomplete && (
          <Alert
            severity="success"
            sx={{ mt: 3, bgcolor: alpha(colors.success, 0.1), border: `1px solid ${alpha(colors.success, 0.3)}` }}
          >
            No applicable cost optimization findings detected for the resources currently in this subscription.
            <Typography variant="caption" display="block" sx={{ mt: 0.5, opacity: 0.85 }}>
              Current resource inventory was evaluated against the available optimization recommendations.
            </Typography>
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
          subtitle="Where the savings are, the biggest wins, and every recommendation. Open any one for the full breakdown."
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
