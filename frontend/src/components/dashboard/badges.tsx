import { Box, Chip, Tooltip } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { colors } from "../../theme";
import type { Severity, Finding } from "../../types";
import type { Area } from "./area";
import { AREA_ACCENT, IMPACT_LABEL, impactToken } from "./tokens";

export function ImpactChip({ severity }: { severity: Severity }) {
  const t = impactToken(severity);
  return (
    <Chip
      size="small"
      label={`${IMPACT_LABEL[severity]} impact`}
      icon={<Box sx={{ width: 7, height: 7, borderRadius: "50%", bgcolor: t.solid, ml: 0.9 }} />}
      sx={{
        bgcolor: t.bg,
        color: t.text,
        border: `1px solid ${t.border}`,
        fontWeight: 600,
        "& .MuiChip-icon": { color: t.solid },
      }}
    />
  );
}

export function AreaTag({ area }: { area: Area }) {
  const c = AREA_ACCENT[area];
  return (
    <Chip
      size="small"
      label={area}
      sx={{
        bgcolor: alpha(c, 0.14),
        color: c,
        border: `1px solid ${alpha(c, 0.3)}`,
        fontWeight: 600,
      }}
    />
  );
}

/** Confidence is a real signal from the backend (0–1): how much to trust the estimate. */
export function ConfidenceChip({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100);
  const c = confidence >= 0.8 ? colors.success : confidence >= 0.5 ? colors.warning : colors.error;
  return (
    <Tooltip title="How confident the analysis is in this estimate, based on data volume and freshness.">
      <Chip
        size="small"
        variant="outlined"
        label={`${pct}% confidence`}
        sx={{ color: c, borderColor: alpha(c, 0.4), bgcolor: alpha(c, 0.08), fontWeight: 600 }}
      />
    </Tooltip>
  );
}

/** Validation vs actual Cost Management spend (real backend signal). */
export function ValidationChip({ finding }: { finding: Finding }) {
  const status = finding.validation_status;

  // Un-validated but non-trivial savings → flag it so it doesn't read as corroborated.
  if (!status || status === "unvalidated") {
    if (finding.estimated_savings_monthly <= 0) return null;
    return (
      <Tooltip title="This figure is an estimate that couldn't be matched to a specific billed line item (e.g. a nominal rate for an orphaned resource) — treat it as approximate.">
        <Chip
          size="small"
          variant="outlined"
          label="Estimate"
          sx={{ color: colors.textMuted, borderColor: colors.border, fontWeight: 600 }}
        />
      </Tooltip>
    );
  }

  const isReview = status === "needs_review";
  const c = isReview ? colors.warning : colors.success;

  // The raw variance can be a huge, alarming number (e.g. +4859%) when an estimate sits far above
  // the billed cost. That belongs in the tooltip as context — never as a scary chip label.
  const v = finding.validation_variance_pct;
  const reviewDetail =
    v != null && v > 0
      ? v >= 100
        ? `The estimate is about ${(v / 100 + 1).toFixed(1)}× this resource's actual billed cost, so it's flagged for a human check rather than being presented as fact.`
        : `The estimate runs ~${v.toFixed(0)}% above this resource's actual billed cost, so it's flagged for a human check.`
      : "The estimate doesn't line up with this resource's actual billed cost, so it's flagged for a human check.";

  return (
    <Tooltip
      title={
        isReview
          ? reviewDetail
          : "Grounded in your actual billed cost (Cost Management) — this is a fraction of what you really pay, not a list-price guess."
      }
    >
      <Chip
        size="small"
        label={isReview ? "Needs review" : "Cost-validated"}
        sx={{ bgcolor: alpha(c, 0.14), color: c, border: `1px solid ${alpha(c, 0.3)}`, fontWeight: 600 }}
      />
    </Tooltip>
  );
}
