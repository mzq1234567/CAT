import React from "react";
import { Alert, Box, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
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
