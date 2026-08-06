import {
  Alert, Box, Breadcrumbs, Button, Chip, CircularProgress, Link, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import PictureAsPdfIcon from "@mui/icons-material/PictureAsPdf";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline";
import Layout from "../components/Layout";
import AssessmentDashboard from "../components/dashboard/AssessmentDashboard";
import AssessmentProgress from "../components/assessment/AssessmentProgress";
import { useApi } from "../services/api";
import { colors } from "../theme";
import React from "react";
import { AssessmentStatus } from "../types";

const STATUS_CHIP: Record<AssessmentStatus, { label: string; color: string }> = {
  queued: { label: "Queued", color: colors.warning },
  fetching_resources: { label: "Collecting", color: colors.info },
  fetching_metrics: { label: "Metrics", color: colors.info },
  running_advisor: { label: "Advisor", color: colors.info },
  calculating_prices: { label: "Pricing", color: colors.info },
  detecting_findings: { label: "Analysing", color: colors.info },
  generating_report: { label: "Finalising", color: colors.info },
  completed: { label: "Completed", color: colors.success },
  failed: { label: "Failed", color: colors.error },
};

export default function Results() {
  const { id } = useParams<{ id: string }>();
  const assessmentId = parseInt(id!, 10);
  const api = useApi();
  const navigate = useNavigate();
  const [downloading, setDownloading] = React.useState(false);

  const { data: assessment, error } = useQuery({
    queryKey: ["assessment", assessmentId],
    queryFn: () => api.getAssessment(assessmentId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      // Poll faster while running so phase transitions and discovered counts land promptly — the
      // progress card animates between polls, but the underlying data should still feel current.
      return status === "completed" || status === "failed" ? false : 2000;
    },
  });

  const handleDownload = async () => {
    setDownloading(true);
    try {
      await api.downloadReport(assessmentId);
    } finally {
      setDownloading(false);
    }
  };

  if (error) {
    return (
      <Layout title={`Assessment #${assessmentId}`}>
        <Alert severity="error" icon={<ErrorOutlineIcon />}>
          Could not load assessment. {String((error as Error).message)}
        </Alert>
      </Layout>
    );
  }

  if (!assessment) {
    return (
      <Layout title="Loading assessment…">
        <Box display="flex" justifyContent="center" mt={10}>
          <CircularProgress />
        </Box>
      </Layout>
    );
  }

  const isTerminal = assessment.status === "completed" || assessment.status === "failed";

  // While running, the animation IS the page — no header, chips or breadcrumbs competing with it.
  if (!isTerminal) {
    return (
      <Layout>
        <AssessmentProgress assessment={assessment} />
      </Layout>
    );
  }

  const chip = STATUS_CHIP[assessment.status];

  const breadcrumbs = (
    <Breadcrumbs sx={{ mb: 1, fontSize: 13 }}>
      <Link
        underline="hover"
        color="text.secondary"
        sx={{ cursor: "pointer" }}
        onClick={() => navigate("/subscriptions")}
      >
        Assessments
      </Link>
      <Typography variant="body2" color={colors.textPrimary}>
        Assessment #{assessment.id}
      </Typography>
    </Breadcrumbs>
  );

  const actions =
    assessment.status === "completed" ? (
      <Button
        variant="contained"
        startIcon={downloading ? <CircularProgress size={16} color="inherit" /> : <PictureAsPdfIcon />}
        onClick={handleDownload}
        disabled={downloading}
      >
        Download PDF
      </Button>
    ) : undefined;

  return (
    <Layout>
      {/* Header */}
      <Box
        display="flex"
        justifyContent="space-between"
        alignItems="flex-start"
        flexWrap="wrap"
        gap={2}
        mb={4}
      >
        <Box>
          {breadcrumbs}
          <Box display="flex" alignItems="center" gap={1.5}>
            <Typography variant="h4" fontWeight={700} color={colors.textPrimary}>
              Assessment #{assessment.id}
            </Typography>
            <Chip
              label={chip.label}
              size="small"
              icon={
                <Box sx={{ width: 8, height: 8, borderRadius: "50%", bgcolor: chip.color, ml: 1 }} />
              }
              sx={{
                bgcolor: alpha(chip.color, 0.15),
                color: chip.color,
                border: `1px solid ${alpha(chip.color, 0.3)}`,
                "& .MuiChip-icon": { color: chip.color },
              }}
            />
          </Box>
          <Typography variant="body2" color="text.secondary" mt={0.5}>
            {assessment.user_email} · {new Date(assessment.created_at).toLocaleString()} ·{" "}
            {assessment.subscription_ids.length} subscription
            {assessment.subscription_ids.length !== 1 ? "s" : ""}
          </Typography>
        </Box>
        {actions && <Box display="flex" gap={1.5}>{actions}</Box>}
      </Box>

      {assessment.status === "failed" && (
        <Alert severity="error" icon={<ErrorOutlineIcon />}>
          Assessment failed: {assessment.error_message}
        </Alert>
      )}

      {/* Results — executive cost-assessment dashboard (real assessment data) */}
      {assessment.status === "completed" && <AssessmentDashboard assessment={assessment} />}
    </Layout>
  );
}
