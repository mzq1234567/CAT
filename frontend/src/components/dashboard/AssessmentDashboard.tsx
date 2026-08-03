import React from "react";
import { Alert, Box, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import TravelExploreIcon from "@mui/icons-material/TravelExplore";
import { colors } from "../../theme";
import type { Assessment } from "../../types";
import { Area, areaForCategory, rollupByArea } from "./area";
import { SectionHeader } from "./primitives";
import ExecutiveSummary from "./ExecutiveSummary";
import InsightsRow from "./InsightsRow";
import AreaBreakdown from "./AreaBreakdown";
import DetailedFindings from "./DetailedFindings";
import RecommendationCard from "./RecommendationCard";
import { setReportCurrency } from "./tokens";

export default function AssessmentDashboard({ assessment }: { assessment: Assessment }) {
  // Dismissed findings drop out of every view; the backend re-rolls the headline totals on dismiss.
  const findings = assessment.findings.filter((f) => !f.dismissed);
  const [area, setArea] = React.useState<Area | null>(null);

  // Render every figure in the subscription's billing currency (detected from Cost Management).
  setReportCurrency(assessment.currency);

  const rollups = React.useMemo(() => rollupByArea(findings), [findings]);

  const totalSavings = assessment.total_savings_annual;

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
      {/* Coverage banner — the full inventory that was scanned */}
      {assessment.total_resources != null && assessment.total_resources > 0 && (
        <Box
          display="flex"
          alignItems="center"
          gap={1}
          mb={2.5}
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

      {/* 01 — the headline: how much can be saved */}
      <Box mb={4}>
        <ExecutiveSummary assessment={assessment} />
      </Box>

      {/* 01b — composition + impact charts */}
      <Box mb={5}>
        <InsightsRow rollups={rollups} totalSavings={totalSavings} findings={findings} />
      </Box>

      {/* 02 — where the savings are (real, clickable filter) */}
      <Box mb={5}>
        <SectionHeader
          title="Where the Savings Are"
          subtitle="Identified savings by area. Select one to focus the recommendations below."
        />
        <AreaBreakdown
          rollups={rollups}
          total={totalSavings}
          selected={area}
          onSelect={setArea}
          spendByArea={assessment.spend_by_area}
        />
      </Box>

      {/* 03 — what to do and why */}
      <Box>
        <SectionHeader
          title="Recommendations"
          subtitle="Sorted by annual savings. Expand any item to see why it was flagged and what to change."
        />
        <DetailedFindings
          findings={filtered}
          renderCard={(f) => <RecommendationCard key={f.id} finding={f} assessmentId={assessment.id} />}
        />
      </Box>
    </Box>
  );
}
