import type { ReactNode } from "react";
import { Box, Button, Card, CardContent, Chip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import { colors } from "../../theme";
import type { Finding } from "../../types";
import { areaForCategory, isConditionalSaving, isReviewFinding } from "./area";
import { affectedResources, affectedLabel, metaFor } from "./categoryMeta";
import { AreaTag } from "./badges";
import { AREA_ACCENT, SAVINGS_COLOR, fmtCompact, fmtUSD } from "./tokens";

/** A single, small confidence label — at most one per card, so a saving never reads as more (or less)
 *  certain than it is. AHB is conditional ("Potential"); a run-rated figure is an "Estimate". */
function ConfLabel({ finding }: { finding: Finding }) {
  const d = (finding.details || {}) as Record<string, unknown>;
  if (isReviewFinding(finding)) {
    return <Tag color={colors.textMuted}>Review</Tag>;
  }
  if (isConditionalSaving(finding.category)) {
    return <Tag color={colors.warning}>Potential</Tag>;
  }
  if (d.cost_anomaly === true) {
    return <Tag color={colors.error}>Verify billing</Tag>;
  }
  if (d.cost_is_estimate === true || d.partial_billing === true) {
    return <Tag color={colors.accentBlue}>Estimate</Tag>;
  }
  return null;
}

function Tag({ color, children }: { color: string; children: ReactNode }) {
  return (
    <Chip
      size="small"
      label={children}
      sx={{
        height: 22, fontSize: 11, fontWeight: 700,
        bgcolor: alpha(color, 0.12), color, border: `1px solid ${alpha(color, 0.3)}`,
      }}
    />
  );
}

function clamp(lines: number) {
  return { display: "-webkit-box", WebkitLineClamp: lines, WebkitBoxOrient: "vertical" as const, overflow: "hidden" };
}

export default function OpportunityCard({
  finding,
  variant,
  onViewDetails,
}: {
  finding: Finding;
  variant: "featured" | "compact";
  onViewDetails: () => void;
}) {
  const area = areaForCategory(finding.category);
  const accent = AREA_ACCENT[area];
  const meta = metaFor(finding.category);
  const conditional = isConditionalSaving(finding.category);
  const count = affectedResources(finding).count;
  // Advisor findings carry a specific, real recommendation — show that, not the generic category blurb.
  const summary =
    finding.category === "advisor_cost"
      ? finding.recommendation || meta.summary(Math.max(count, 1))
      : meta.summary(Math.max(count, 1));

  const featured = variant === "featured";
  const review = isReviewFinding(finding);
  const d = (finding.details || {}) as Record<string, unknown>;
  const referencePrice = typeof d.reference_monthly_price === "number" ? d.reference_monthly_price : null;

  return (
    <Card
      sx={{
        position: "relative", overflow: "hidden", height: "100%",
        transition: "box-shadow .2s ease, border-color .2s ease",
        "&:hover": {
          boxShadow: `0 8px 24px ${alpha(accent, featured ? 0.16 : 0.1)}`,
          borderColor: alpha(accent, 0.45),
        },
      }}
    >
      <Box sx={{ position: "absolute", left: 0, top: 0, bottom: 0, width: featured ? 4 : 3, bgcolor: accent }} />
      <CardContent
        sx={{
          p: featured ? 3 : 2.25, pl: featured ? 3.25 : 2.6,
          height: "100%", display: "flex", flexDirection: "column",
        }}
      >
        {/* Top row — category · (one confidence label). No "impact" severity: a cost opportunity is
            ranked by the size of its saving, not a risk rating. */}
        <Box display="flex" gap={0.75} mb={1.25} flexWrap="wrap" alignItems="center">
          <AreaTag area={area} />
          <ConfLabel finding={finding} />
        </Box>

        {/* Title */}
        <Typography
          variant={featured ? "h6" : "subtitle1"}
          fontWeight={featured ? 800 : 700}
          color={colors.textPrimary}
          sx={{ lineHeight: 1.25, ...clamp(2) }}
        >
          {finding.display_name}
        </Typography>

        {/* One concise sentence */}
        <Typography variant="body2" color="text.secondary" mt={0.75} mb={2} sx={{ lineHeight: 1.5, ...clamp(2) }}>
          {summary}
        </Typography>

        {/* Bottom — savings (or "Not quantified" for a REVIEW finding) + action */}
        <Box display="flex" alignItems="flex-end" justifyContent="space-between" gap={2} mt="auto">
          {review ? (
            <Box minWidth={0}>
              <Typography
                fontWeight={800}
                color={colors.textSecondary}
                sx={{ fontSize: featured ? "1.25rem" : "1.05rem", lineHeight: 1.15 }}
              >
                Not quantified
              </Typography>
              <Typography variant="caption" color={colors.textMuted} sx={{ display: "block", mt: 0.25 }}>
                Financial impact needs review
                {referencePrice != null && <> · ref. list {fmtUSD(referencePrice)} / mo</>}
                {count > 0 && <> · {affectedLabel(finding)}</>}
              </Typography>
            </Box>
          ) : (
            <Box minWidth={0}>
              <Typography
                fontWeight={800}
                color={SAVINGS_COLOR}
                sx={{ fontSize: featured ? "2rem" : "1.4rem", lineHeight: 1, fontVariantNumeric: "tabular-nums" }}
              >
                {fmtCompact(finding.estimated_savings_annual)}
              </Typography>
              <Typography variant="caption" color={colors.textMuted} sx={{ display: "block", mt: 0.25 }}>
                {conditional ? "potential / year" : "per year"}
                {featured && <> · {fmtUSD(finding.estimated_savings_monthly)} / mo</>}
                {count > 0 && <> · {affectedLabel(finding)}</>}
              </Typography>
            </Box>
          )}
          {featured ? (
            <Button variant="contained" onClick={onViewDetails} endIcon={<ArrowForwardIcon />} sx={{ flexShrink: 0 }}>
              View details
            </Button>
          ) : (
            <Button
              size="small"
              onClick={onViewDetails}
              endIcon={<ArrowForwardIcon sx={{ fontSize: 16 }} />}
              sx={{ textTransform: "none", color: colors.accentBlue, flexShrink: 0 }}
            >
              View details
            </Button>
          )}
        </Box>
      </CardContent>
    </Card>
  );
}
