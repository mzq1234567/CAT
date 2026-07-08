import {
  Alert, Box, Breadcrumbs, Button, Chip, CircularProgress,
  LinearProgress, Link, Stack, Typography,
} from "@mui/material";
import { alpha, keyframes } from "@mui/material/styles";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import TableChartIcon from "@mui/icons-material/TableChart";
import PictureAsPdfIcon from "@mui/icons-material/PictureAsPdf";
import CloudSyncIcon from "@mui/icons-material/CloudSync";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import RadioButtonUncheckedIcon from "@mui/icons-material/RadioButtonUnchecked";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline";
import Layout from "../components/Layout";
import SummaryCards from "../components/SummaryCards";
import FindingsTable from "../components/FindingsTable";
import { useApi } from "../services/api";
import { colors, SEVERITY } from "../theme";
import React from "react";
import { AssessmentStatus } from "../types";

const pulse = keyframes`
  0% { transform: scale(1); opacity: 0.85; }
  50% { transform: scale(1.12); opacity: 1; }
  100% { transform: scale(1); opacity: 0.85; }
`;

const STATUS_CHIP: Record<AssessmentStatus, { label: string; color: string }> = {
  pending: { label: "Queued", color: colors.warning },
  running: { label: "Running", color: colors.info },
  completed: { label: "Completed", color: colors.success },
  failed: { label: "Failed", color: colors.error },
};

const PHASES = [
  { key: "pending", label: "Queued for processing" },
  { key: "running", label: "Collecting Azure inventory" },
  { key: "running2", label: "Evaluating cost findings" },
  { key: "completed", label: "Generating report" },
];

function ProgressView({ status }: { status: AssessmentStatus }) {
  // Map status to an approximate progress + active phase index.
  const phaseIndex = status === "pending" ? 0 : status === "running" ? 2 : 4;
  const pct = status === "pending" ? 12 : status === "running" ? 62 : 95;

  return (
    <Box display="flex" justifyContent="center" mt={2}>
      <Box
        sx={{
          maxWidth: 480,
          width: "100%",
          bgcolor: colors.surface,
          border: `1px solid ${colors.border}`,
          borderRadius: 4,
          p: 4,
          textAlign: "center",
        }}
      >
        <Box
          sx={{
            width: 72,
            height: 72,
            mx: "auto",
            mb: 3,
            borderRadius: "50%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background: "linear-gradient(135deg, #3B82F6 0%, #6366F1 100%)",
            animation: `${pulse} 2s ease-in-out infinite`,
          }}
        >
          <CloudSyncIcon sx={{ color: "#fff", fontSize: 38 }} />
        </Box>

        <Typography variant="h6" fontWeight={700} color={colors.textPrimary} gutterBottom>
          Running your assessment
        </Typography>
        <Typography variant="body2" color="text.secondary" mb={3}>
          We are analysing your Azure resources for cost optimisation opportunities.
          This may take a few minutes.
        </Typography>

        <Box mb={3}>
          <LinearProgress
            variant="determinate"
            value={pct}
            sx={{ "& .MuiLinearProgress-bar": { borderRadius: 4 } }}
          />
          <Typography variant="caption" color="text.secondary" mt={1} display="block">
            {pct}% complete
          </Typography>
        </Box>

        <Stack spacing={1.25} sx={{ textAlign: "left" }}>
          {PHASES.map((phase, i) => {
            const done = i < phaseIndex;
            const active = i === phaseIndex || (i === phaseIndex - 1 && status === "running");
            return (
              <Box key={phase.key} display="flex" alignItems="center" gap={1.5}>
                {done ? (
                  <CheckCircleIcon sx={{ fontSize: 18, color: colors.success }} />
                ) : active ? (
                  <CircularProgress size={16} thickness={6} />
                ) : (
                  <RadioButtonUncheckedIcon sx={{ fontSize: 18, color: colors.textMuted }} />
                )}
                <Typography
                  variant="body2"
                  color={done || active ? colors.textPrimary : colors.textMuted}
                  fontWeight={active ? 600 : 400}
                >
                  {phase.label}
                </Typography>
              </Box>
            );
          })}
        </Stack>
      </Box>
    </Box>
  );
}

export default function Results() {
  const { id } = useParams<{ id: string }>();
  const assessmentId = parseInt(id!, 10);
  const api = useApi();
  const navigate = useNavigate();
  const [downloading, setDownloading] = React.useState<"pdf" | "excel" | null>(null);

  const { data: assessment, error } = useQuery({
    queryKey: ["assessment", assessmentId],
    queryFn: () => api.getAssessment(assessmentId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "completed" || status === "failed" ? false : 4000;
    },
  });

  const { data: byCategory } = useQuery({
    queryKey: ["findings-by-category", assessmentId],
    queryFn: () => api.getFindingsByCategory(assessmentId),
    enabled: assessment?.status === "completed",
  });

  const handleDownload = async (format: "pdf" | "excel") => {
    setDownloading(format);
    try {
      await api.downloadReport(assessmentId, format);
    } finally {
      setDownloading(null);
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
      <>
        <Button
          variant="outlined"
          color="inherit"
          startIcon={downloading === "pdf" ? <CircularProgress size={16} /> : <PictureAsPdfIcon />}
          onClick={() => handleDownload("pdf")}
          disabled={downloading !== null}
          sx={{ borderColor: colors.border, color: colors.textSecondary }}
        >
          Download PDF
        </Button>
        <Button
          variant="contained"
          startIcon={
            downloading === "excel" ? <CircularProgress size={16} color="inherit" /> : <TableChartIcon />
          }
          onClick={() => handleDownload("excel")}
          disabled={downloading !== null}
        >
          Download Excel
        </Button>
      </>
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

      {/* Progress state */}
      {!isTerminal && <ProgressView status={assessment.status} />}

      {assessment.status === "failed" && (
        <Alert severity="error" icon={<ErrorOutlineIcon />}>
          Assessment failed: {assessment.error_message}
        </Alert>
      )}

      {/* Results */}
      {assessment.status === "completed" && (
        <>
          <SummaryCards assessment={assessment} byCategory={byCategory ?? []} />
          {assessment.findings.length > 0 ? (
            <FindingsTable findings={assessment.findings} />
          ) : (
            <Alert
              severity="success"
              sx={{
                bgcolor: SEVERITY.low.bg,
                border: `1px solid ${alpha(colors.success, 0.3)}`,
              }}
            >
              No cost optimisation findings detected — your environment looks well-optimised!
            </Alert>
          )}
        </>
      )}
    </Layout>
  );
}
