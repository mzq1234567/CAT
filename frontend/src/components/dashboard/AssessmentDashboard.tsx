import React from "react";
import { Alert, Box, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import TravelExploreIcon from "@mui/icons-material/TravelExplore";
import { colors } from "../../theme";
import type { Assessment } from "../../types";
import { Area, areaForCategory } from "./area";
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

  const totalSavings = assessment.total_savings_annual;
  const excluded = React.useMemo(
    () => assessment.findings.filter((f) => f.dismissed),
    [assessment.findings]
  );

  const filtered = React.useMemo(
    () => (area ? findings.filter((f) => areaForCategory(f.category) === area) : findings),
    [findings, area]
  );

  if (findings.length === 0) {
    return (
      <>
        <ExecutiveSummary assessment={assessment} />
        <Alert
          severity="success"
          sx={{ mt: 3, bgcolor: alpha(colors.success, 0.1), border: `1px solid ${alpha(colors.success, 0.3)}` }}
        >
          No cost optimization findings detected — this environment looks well-optimized.
        </Alert>
      </>
    );
  }

  return (
    <Box>
      {/* 01 — the financial headline: three KPI cards (the 5-second story), the focus of the page */}
      <Box mb={3}>
        <ExecutiveSummary assessment={assessment} />
      </Box>

      {/* Coverage banner — the inventory that was scanned (resource breakdown), below the money */}
      {assessment.total_resources != null && assessment.total_resources > 0 && (
        <Box
          display="flex"
          alignItems="center"
          gap={1}
          mb={4}
          px={2}
          py={1.25}
          sx={{
            borderRadius: 2,
            bgcolor: alpha(colors.accentBlue, 0.06),
            border: `1px solid ${alpha(colors.accentBlue, 0.2)}`,
          }}
        >
          <TravelExploreIcon sx={{ fontSize: 18, color: colors.accentBlue }} />
          <Typography variant="body2" color={colors.textSecondary}>
            Scanned{" "}
            <Box component="span" sx={{ color: colors.textPrimary, fontWeight: 700 }}>
              {assessment.total_resources.toLocaleString()}
            </Box>{" "}
            Azure resources across{" "}
            <Box component="span" sx={{ color: colors.textPrimary, fontWeight: 700 }}>
              {assessment.resource_type_count}
            </Box>{" "}
            resource types · {findings.length} optimization{" "}
            {findings.length === 1 ? "opportunity" : "opportunities"} identified.
          </Typography>
        </Box>
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
