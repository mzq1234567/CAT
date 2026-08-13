import React from "react";
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Skeleton, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import AddCircleOutlineIcon from "@mui/icons-material/AddCircleOutline";
import AssessmentOutlinedIcon from "@mui/icons-material/AssessmentOutlined";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import Layout from "../components/Layout";
import { useApi } from "../services/api";
import { errorMessage } from "../services/errors";
import { colors } from "../theme";
import type { AssessmentStatus, AssessmentSummary } from "../types";

const STATUS: Record<AssessmentStatus, { label: string; color: string }> = {
  queued: { label: "Queued", color: colors.warning },
  fetching_resources: { label: "Running", color: colors.info },
  fetching_metrics: { label: "Running", color: colors.info },
  running_advisor: { label: "Running", color: colors.info },
  calculating_prices: { label: "Running", color: colors.info },
  detecting_findings: { label: "Running", color: colors.info },
  generating_report: { label: "Running", color: colors.info },
  completed: { label: "Completed", color: colors.success },
  failed: { label: "Failed", color: colors.error },
};

function money(n: number | null | undefined, currency: string): string {
  if (n == null) return "—";
  try {
    return new Intl.NumberFormat("en", { style: "currency", currency, maximumFractionDigits: 0 }).format(n);
  } catch {
    return `${Math.round(n).toLocaleString()}`;
  }
}

function AssessmentRow({ a, onOpen }: { a: AssessmentSummary; onOpen: () => void }) {
  const status = STATUS[a.status] ?? STATUS.queued;
  const running = a.status !== "completed" && a.status !== "failed";
  return (
    <Box
      role="button"
      onClick={onOpen}
      sx={{
        display: "flex",
        alignItems: "center",
        gap: 2,
        p: 2,
        borderRadius: 2,
        cursor: "pointer",
        border: `1px solid ${colors.border}`,
        bgcolor: colors.surface,
        transition: "border-color .15s ease, box-shadow .2s ease",
        "&:hover": { borderColor: colors.accentBlue, boxShadow: `0 6px 20px ${alpha(colors.accentBlue, 0.1)}` },
      }}
    >
      <Box
        sx={{
          width: 40, height: 40, borderRadius: 2, flexShrink: 0, display: "flex",
          alignItems: "center", justifyContent: "center",
          bgcolor: alpha(colors.accentBlue, 0.12), color: colors.accentBlue,
        }}
      >
        <AssessmentOutlinedIcon fontSize="small" />
      </Box>

      <Box flex={1} minWidth={0}>
        <Box display="flex" alignItems="center" gap={1} flexWrap="wrap">
          <Typography variant="body2" fontWeight={700} color={colors.textPrimary}>
            Assessment #{a.id}
          </Typography>
          <Chip
            label={status.label}
            size="small"
            icon={running ? <CircularProgress size={11} sx={{ color: `${status.color} !important`, ml: 1 }} /> : undefined}
            sx={{
              height: 20, fontSize: 11, fontWeight: 700,
              bgcolor: alpha(status.color, 0.14), color: status.color,
              border: `1px solid ${alpha(status.color, 0.3)}`,
            }}
          />
        </Box>
        <Typography variant="caption" color={colors.textMuted} noWrap>
          {new Date(a.created_at).toLocaleString()} · {a.subscription_ids.length} subscription
          {a.subscription_ids.length !== 1 ? "s" : ""}
          {a.status === "completed" && (
            <> · {a.findings_count} optimization {a.findings_count === 1 ? "opportunity" : "opportunities"}</>
          )}
        </Typography>
      </Box>

      {a.status === "completed" && a.total_savings_annual > 0 && (
        <Box textAlign="right" sx={{ display: { xs: "none", sm: "block" } }}>
          <Typography variant="body2" fontWeight={800} sx={{ color: colors.success, lineHeight: 1.1 }}>
            {money(a.total_savings_annual, a.currency)}
          </Typography>
          <Typography variant="caption" color={colors.textMuted}>
            / year
          </Typography>
        </Box>
      )}

      <ChevronRightIcon sx={{ color: colors.textMuted, flexShrink: 0 }} />
    </Box>
  );
}

export default function Assessments() {
  const api = useApi();
  const navigate = useNavigate();

  const { data, isLoading, error } = useQuery({
    queryKey: ["assessments"],
    queryFn: () => api.listAssessments(),
    // Keep the list fresh while any assessment is still running.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((a) => a.status !== "completed" && a.status !== "failed") ? 3000 : false,
  });

  const items = data ?? [];

  return (
    <Layout
      title="Assessments"
      subtitle="Your Azure cost assessments. Open one to review its optimization opportunities."
      actions={
        <Button variant="contained" startIcon={<AddCircleOutlineIcon />} onClick={() => navigate("/subscriptions")}>
          New assessment
        </Button>
      }
    >
      <Box maxWidth={820}>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {errorMessage(error)}
          </Alert>
        )}

        {isLoading && (
          <Box display="flex" flexDirection="column" gap={1.25}>
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} variant="rounded" height={74} sx={{ bgcolor: colors.surfaceElevated }} />
            ))}
          </Box>
        )}

        {!isLoading && !error && items.length === 0 && (
          <Card>
            <CardContent sx={{ textAlign: "center", py: 6 }}>
              <AssessmentOutlinedIcon sx={{ fontSize: 48, color: colors.textMuted, mb: 1.5 }} />
              <Typography variant="h6" color={colors.textPrimary} gutterBottom>
                No assessments yet
              </Typography>
              <Typography variant="body2" color="text.secondary" maxWidth={420} mx="auto" mb={3}>
                Run your first Azure cost assessment to discover where spend can be optimized.
              </Typography>
              <Button variant="contained" startIcon={<AddCircleOutlineIcon />} onClick={() => navigate("/subscriptions")}>
                New assessment
              </Button>
            </CardContent>
          </Card>
        )}

        {!isLoading && items.length > 0 && (
          <Box display="flex" flexDirection="column" gap={1.25}>
            {items.map((a) => (
              <AssessmentRow key={a.id} a={a} onOpen={() => navigate(`/assessments/${a.id}`)} />
            ))}
          </Box>
        )}
      </Box>
    </Layout>
  );
}
