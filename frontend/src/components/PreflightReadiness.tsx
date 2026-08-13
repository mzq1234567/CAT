import React from "react";
import { Box, Typography } from "@mui/material";
import { alpha, keyframes } from "@mui/material/styles";
import { useQueries } from "@tanstack/react-query";
import CheckCircleRoundedIcon from "@mui/icons-material/CheckCircleRounded";
import WarningAmberRoundedIcon from "@mui/icons-material/WarningAmberRounded";
import ErrorOutlineRoundedIcon from "@mui/icons-material/ErrorOutlineRounded";
import CloudDoneRoundedIcon from "@mui/icons-material/CloudDoneRounded";
import { useApi } from "../services/api";
import { useReducedMotion } from "./assessment/useAssessmentMotion";
import { colors } from "../theme";
import { PreflightCheck, PreflightResponse, Subscription } from "../types";

/**
 * Pre-flight readiness — a calm, professional "environment check" before an assessment runs.
 *
 * Every state shown is the REAL backend result (see services/preflight.py): the friendly labels and
 * copy below only translate that result into the customer's language — no diagnostic terminology, no
 * raw API errors, and no faked success. While the check is in flight we sequence the rows so it reads
 * as a considered pre-flight rather than a spinner; the real states replace the sequence the instant
 * they arrive. `onReadyChange` still fires exactly as before (all subscriptions ready → true).
 */

// Backend check key → customer-facing label + the wording used when it's available.
const META: Record<string, { label: string; ok: string }> = {
  signin: { label: "Microsoft account", ok: "Connected" },
  subscription: { label: "Azure subscriptions", ok: "Access confirmed" },
  inventory: { label: "Azure resources", ok: "Ready to review" },
  cost: { label: "Cost data", ok: "Available" },
  metrics: { label: "Resource utilization", ok: "Available" },
  pricing: { label: "Azure pricing", ok: "Available" },
};
const ORDER = ["signin", "subscription", "inventory", "cost", "metrics", "pricing"];

type Tone = "ok" | "warning" | "error" | "pending";

// Translate a real backend check into a client-safe { tone, value, note }. Never exposes raw detail.
function resolve(check: PreflightCheck): { tone: Tone; value: string; note?: string } {
  const meta = META[check.key];
  const okLabel = meta?.ok ?? "Available";
  if (check.status === "ok") return { tone: "ok", value: okLabel };
  if (check.status === "warning") {
    if (check.key === "cost")
      return {
        tone: "warning",
        value: "Temporarily unavailable",
        note: "Some savings opportunities may be shown without financial estimates.",
      };
    if (check.key === "pricing")
      return { tone: "warning", value: "Currently limited", note: "Pricing-based figures may be limited for this run." };
    return { tone: "warning", value: "Temporarily unavailable" };
  }
  // unavailable
  if (check.blocking) {
    const note =
      check.key === "subscription"
        ? "We couldn't reach this subscription with your account."
        : check.key === "inventory"
        ? "We couldn't review the resources in this subscription."
        : "This needs attention before the assessment can run.";
    return { tone: "error", value: "Action needed", note };
  }
  return { tone: "pending", value: "Not checked" };
}

const TONE_COLOR: Record<Tone, string> = {
  ok: colors.success,
  warning: colors.warning,
  error: colors.error,
  pending: colors.textMuted,
};

const sweep = keyframes`
  0%, 100% { opacity: 0.35; }
  50%      { opacity: 1; }
`;
const revealIn = keyframes`
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: none; }
`;

function ToneIcon({ tone }: { tone: Tone }) {
  const c = TONE_COLOR[tone];
  const sx = { fontSize: 20, color: c, flexShrink: 0 };
  if (tone === "ok") return <CheckCircleRoundedIcon sx={sx} />;
  if (tone === "warning") return <WarningAmberRoundedIcon sx={sx} />;
  if (tone === "error") return <ErrorOutlineRoundedIcon sx={sx} />;
  return <Box sx={{ width: 20, display: "flex", justifyContent: "center" }}>
    <Box sx={{ width: 6, height: 6, borderRadius: "50%", bgcolor: colors.textMuted, opacity: 0.5 }} />
  </Box>;
}

/** One elegant status row: label on the left, value (+ optional note) on the right. */
function StatusRow({
  label,
  tone,
  value,
  note,
  index,
  reduced,
}: {
  label: string;
  tone: Tone;
  value: string;
  note?: string;
  index: number;
  reduced: boolean;
}) {
  return (
    <Box
      sx={{
        display: "flex",
        alignItems: "flex-start",
        gap: 1.5,
        py: 1.5,
        borderTop: index === 0 ? "none" : `1px solid ${colors.border}`,
        opacity: reduced ? 1 : 0,
        animation: reduced ? "none" : `${revealIn} .45s cubic-bezier(.16,1,.3,1) ${index * 70}ms forwards`,
      }}
    >
      <Box sx={{ mt: "1px" }}>
        <ToneIcon tone={tone} />
      </Box>
      <Box flex={1} minWidth={0}>
        <Typography variant="body2" fontWeight={600} color={colors.textPrimary}>
          {label}
        </Typography>
        {note && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.25, lineHeight: 1.5 }}>
            {note}
          </Typography>
        )}
      </Box>
      <Typography
        variant="body2"
        sx={{ fontWeight: 600, color: TONE_COLOR[tone], whiteSpace: "nowrap", mt: "1px" }}
      >
        {value}
      </Typography>
    </Box>
  );
}

/** The in-flight state: the known checks, revealed in a calm sequence with a travelling highlight. */
function CheckingRows({ activeStep, reduced }: { activeStep: number; reduced: boolean }) {
  return (
    <Box>
      {ORDER.map((key, i) => {
        const active = i === activeStep;
        const done = i < activeStep;
        return (
          <Box
            key={key}
            sx={{
              display: "flex",
              alignItems: "center",
              gap: 1.5,
              py: 1.5,
              borderTop: i === 0 ? "none" : `1px solid ${colors.border}`,
            }}
          >
            <Box sx={{ width: 20, display: "flex", justifyContent: "center" }}>
              <Box
                sx={{
                  width: 8,
                  height: 8,
                  borderRadius: "50%",
                  bgcolor: done ? colors.accentBlue : active ? colors.accentBlue : colors.border,
                  opacity: done ? 0.55 : 1,
                  animation: !reduced && active ? `${sweep} 1.1s ease-in-out infinite` : "none",
                }}
              />
            </Box>
            <Typography
              variant="body2"
              sx={{ flex: 1, fontWeight: 600, color: active ? colors.textPrimary : colors.textSecondary }}
            >
              {META[key].label}
            </Typography>
            <Typography variant="caption" sx={{ color: colors.textMuted }}>
              {done ? "Checked" : active ? "Checking…" : "Pending"}
            </Typography>
          </Box>
        );
      })}
    </Box>
  );
}

function SubscriptionPanel({
  sub,
  report,
  isLoading,
  isError,
  activeStep,
  multi,
  reduced,
}: {
  sub: Subscription;
  report?: PreflightResponse;
  isLoading: boolean;
  isError: boolean;
  activeStep: number;
  multi: boolean;
  reduced: boolean;
}) {
  const ordered = report
    ? [...report.checks].sort((a, b) => ORDER.indexOf(a.key) - ORDER.indexOf(b.key))
    : [];
  return (
    <Box
      sx={{
        border: `1px solid ${colors.border}`,
        borderRadius: 3,
        p: { xs: 2, md: 2.5 },
        bgcolor: colors.surface,
      }}
    >
      {multi && (
        <Typography variant="body2" fontWeight={700} color={colors.textPrimary} noWrap sx={{ mb: 0.5 }}>
          {report?.subscription_name || sub.display_name}
        </Typography>
      )}
      {isError ? (
        <Typography variant="body2" color="text.secondary" sx={{ py: 1 }}>
          We couldn't complete the check for this subscription. Please try again.
        </Typography>
      ) : isLoading || !report ? (
        <CheckingRows activeStep={activeStep} reduced={reduced} />
      ) : (
        ordered.map((check, i) => {
          const r = resolve(check);
          return (
            <StatusRow
              key={check.key}
              label={META[check.key]?.label ?? check.label}
              tone={r.tone}
              value={r.value}
              note={r.note}
              index={i}
              reduced={reduced}
            />
          );
        })
      )}
    </Box>
  );
}

export default function PreflightReadiness({
  subscriptions,
  onReadyChange,
}: {
  subscriptions: Subscription[];
  onReadyChange: (ready: boolean) => void;
}) {
  const api = useApi();
  const reduced = useReducedMotion();

  const results = useQueries({
    queries: subscriptions.map((sub) => ({
      queryKey: ["preflight", sub.id],
      queryFn: () => api.preflight(sub.id),
      staleTime: 60_000,
      retry: 1,
    })),
  });

  // Ready only when every selected subscription resolved AND reports ready (no blocking failure).
  const allReady = subscriptions.length > 0 && results.every((r) => r.isSuccess && r.data?.ready === true);
  React.useEffect(() => {
    onReadyChange(allReady);
  }, [allReady, onReadyChange]);

  const anyLoading = results.some((r) => r.isLoading);
  const anyError = results.some((r) => r.isError);

  // Drive the in-flight sequence (presentation only — the real states replace it on arrival).
  const [activeStep, setActiveStep] = React.useState(0);
  React.useEffect(() => {
    if (!anyLoading || reduced) return;
    const t = setInterval(() => setActiveStep((s) => Math.min(s + 1, ORDER.length - 1)), 620);
    return () => clearInterval(t);
  }, [anyLoading, reduced]);
  React.useEffect(() => {
    if (!anyLoading) setActiveStep(0);
  }, [anyLoading]);

  // Aggregate state → heading + supporting copy (customer language, never diagnostic).
  const heading =
    anyLoading
      ? { icon: null, title: "Preparing your Azure assessment", copy: "We’re securely confirming access to the selected environment and the data needed to build your assessment." }
      : allReady
      ? { icon: <CloudDoneRoundedIcon sx={{ fontSize: 26, color: colors.success }} />, title: "Your Azure environment is ready", copy: "Access is confirmed. We’re ready to review the selected environment and identify opportunities to reduce your Azure spend." }
      : anyError
      ? { icon: <ErrorOutlineRoundedIcon sx={{ fontSize: 26, color: colors.error }} />, title: "We couldn’t complete the check", copy: "Something interrupted the environment check. You can change your selection and try again." }
      : { icon: <WarningAmberRoundedIcon sx={{ fontSize: 26, color: colors.warning }} />, title: "Access needs attention", copy: "One or more selected subscriptions couldn’t be reached with your account. Review the details below, or change your selection and try again." };

  const multi = subscriptions.length > 1;

  return (
    <Box>
      <Box display="flex" alignItems="flex-start" gap={1.5} mb={2.5}>
        {heading.icon && <Box sx={{ mt: "2px" }}>{heading.icon}</Box>}
        <Box>
          <Typography variant="h6" fontWeight={800} color={colors.textPrimary} sx={{ letterSpacing: "-0.01em" }}>
            {heading.title}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, lineHeight: 1.6, maxWidth: 620 }}>
            {heading.copy}
          </Typography>
        </Box>
      </Box>

      <Box display="flex" flexDirection="column" gap={1.5}>
        {subscriptions.map((sub, i) => {
          const r = results[i];
          return (
            <SubscriptionPanel
              key={sub.id}
              sub={sub}
              report={r?.data}
              isLoading={r?.isLoading ?? true}
              isError={r?.isError ?? false}
              activeStep={activeStep}
              multi={multi}
              reduced={reduced}
            />
          );
        })}
      </Box>
    </Box>
  );
}
