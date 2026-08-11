import React from "react";
import {
  Box, Button, Chip, Collapse, Dialog, DialogActions, DialogContent, DialogTitle,
  IconButton, Tooltip, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import CloseIcon from "@mui/icons-material/Close";
import AutoGraphOutlinedIcon from "@mui/icons-material/AutoGraphOutlined";
import LightbulbOutlinedIcon from "@mui/icons-material/LightbulbOutlined";
import PaidOutlinedIcon from "@mui/icons-material/PaidOutlined";
import BuildOutlinedIcon from "@mui/icons-material/BuildOutlined";
import RuleOutlinedIcon from "@mui/icons-material/RuleOutlined";
import InsightsOutlinedIcon from "@mui/icons-material/InsightsOutlined";
import DnsOutlinedIcon from "@mui/icons-material/DnsOutlined";
import ReportProblemOutlinedIcon from "@mui/icons-material/ReportProblemOutlined";
import RemoveCircleOutlineIcon from "@mui/icons-material/RemoveCircleOutline";
import VisibilityOutlinedIcon from "@mui/icons-material/VisibilityOutlined";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import KeyboardArrowDownIcon from "@mui/icons-material/KeyboardArrowDown";
import { colors } from "../../theme";
import type { Finding } from "../../types";
import { areaForCategory, isConditionalSaving } from "./area";
import { affectedResources, affectedLabel, metaFor } from "./categoryMeta";
import { AreaTag, ImpactChip, ValidationChip, AdvisorImpactChip } from "./badges";
import FindingEvidence, { hasSupportingMetrics } from "./FindingEvidence";
import { AREA_ACCENT, SAVINGS_COLOR, fmtUSD, fmtPct } from "./tokens";

function Section({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <Box mb={2.75}>
      <Box display="flex" alignItems="center" gap={0.75} mb={1} sx={{ color: colors.textSecondary }}>
        {icon}
        <Typography variant="caption" fontWeight={700} textTransform="uppercase" letterSpacing="0.06em">
          {title}
        </Typography>
      </Box>
      {children}
    </Box>
  );
}

/** A section that starts collapsed — used for the secondary detail so the modal isn't a wall of text. */
function CollapsibleSection({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  const [open, setOpen] = React.useState(false);
  return (
    <Box mb={1} sx={{ borderTop: `1px solid ${colors.border}`, pt: 1.5 }}>
      <Box
        role="button"
        onClick={() => setOpen((o) => !o)}
        display="flex" alignItems="center" gap={0.75}
        sx={{ cursor: "pointer", color: colors.textSecondary, "&:hover": { color: colors.textPrimary } }}
      >
        {icon}
        <Typography variant="caption" fontWeight={700} textTransform="uppercase" letterSpacing="0.06em" flex={1}>
          {title}
        </Typography>
        <KeyboardArrowDownIcon sx={{ fontSize: 18, transform: open ? "rotate(180deg)" : "none", transition: "transform .2s ease" }} />
      </Box>
      <Collapse in={open} timeout="auto" unmountOnExit>
        <Box mt={1.5}>{children}</Box>
      </Collapse>
    </Box>
  );
}

function FinStat({ label, value, accent, big }: { label: string; value: string; accent?: string; big?: boolean }) {
  return (
    <Box>
      <Typography
        fontWeight={800}
        sx={{ color: accent ?? colors.textPrimary, lineHeight: 1.1, fontSize: big ? "1.45rem" : "1.1rem", fontVariantNumeric: "tabular-nums" }}
      >
        {value}
      </Typography>
      <Typography variant="caption" color={colors.textMuted}>{label}</Typography>
    </Box>
  );
}

function AffectedResourcesSection({ finding }: { finding: Finding }) {
  const { count, items } = affectedResources(finding);
  const [show, setShow] = React.useState(false);
  if (count === 0) return null;

  return (
    <Section icon={<DnsOutlinedIcon fontSize="small" />} title="Affected Resources">
      <Box
        display="flex" alignItems="center" justifyContent="space-between" gap={2}
        sx={{ p: 1.5, borderRadius: 2, bgcolor: colors.surfaceElevated, border: `1px solid ${colors.border}` }}
      >
        <Typography variant="body2" fontWeight={700} color={colors.textPrimary}>
          {affectedLabel(finding)}
        </Typography>
        {items.length > 0 && (
          <Button
            size="small"
            onClick={() => setShow((s) => !s)}
            startIcon={show ? <ExpandLessIcon sx={{ fontSize: 16 }} /> : <VisibilityOutlinedIcon sx={{ fontSize: 16 }} />}
            sx={{ textTransform: "none", color: colors.accentBlue }}
          >
            {show ? "Hide resources" : "Show resources"}
          </Button>
        )}
      </Box>
      <Collapse in={show} timeout="auto" unmountOnExit>
        <Box mt={1} display="flex" flexDirection="column" gap={0.25}>
          {items.slice(0, 100).map((v, i) => (
            <Box
              key={`${v.name}-${i}`}
              display="flex" justifyContent="space-between" alignItems="baseline"
              sx={{ px: 1.5, py: 0.85, borderRadius: 1, bgcolor: i % 2 ? "transparent" : colors.surfaceElevated }}
            >
              <Box minWidth={0}>
                <Typography variant="body2" fontWeight={600} color={colors.textPrimary} noWrap>{v.name}</Typography>
                {(v.sku || v.region) && (
                  <Typography variant="caption" color={colors.textMuted} sx={{ fontFamily: "monospace" }}>
                    {v.sku}{v.sku && v.region ? " · " : ""}{v.region}
                  </Typography>
                )}
              </Box>
              {v.monthly_savings != null && v.monthly_savings > 0 && (
                <Typography variant="body2" fontWeight={700} sx={{ color: SAVINGS_COLOR, whiteSpace: "nowrap" }}>
                  {fmtUSD(v.monthly_savings * 12)} / yr
                </Typography>
              )}
            </Box>
          ))}
          {items.length > 100 && (
            <Typography variant="caption" color={colors.textMuted} mt={0.5}>+{items.length - 100} more</Typography>
          )}
        </Box>
      </Collapse>
    </Section>
  );
}

export default function RecommendationDetails({
  finding,
  open,
  onClose,
  onExclude,
}: {
  finding: Finding;
  open: boolean;
  onClose: () => void;
  /** Exclude from the total savings — handled by the parent so it can offer an Undo. */
  onExclude: () => void;
}) {
  const area = areaForCategory(finding.category);
  const accent = AREA_ACCENT[area];
  const meta = metaFor(finding.category);

  const isVm =
    finding.resource_type === "microsoft.compute/virtualmachines" ||
    finding.category === "idle_vms" || finding.category === "oversized_vms";
  const actual = finding.actual_monthly_cost;
  const costReductionPct =
    !isVm && actual && actual > 0 ? Math.min(100, (finding.estimated_savings_monthly / actual) * 100) : null;
  const showMetrics = hasSupportingMetrics(finding);

  // Conditional (Azure Hybrid Benefit) savings only materialise if the customer already OWNS eligible
  // licences — so we label the amount "Potential" and put the prerequisite front-and-centre, never
  // implying the customer gets it automatically.
  const conditional = isConditionalSaving(finding.category);
  const d = (finding.details || {}) as Record<string, unknown>;
  const excludedCount = typeof d.excluded_count === "number" ? d.excluded_count : 0;
  const eligibleCount = typeof d.eligible_count === "number" ? d.eligible_count : undefined;
  const partialBilling = d.partial_billing === true;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth scroll="paper"
      PaperProps={{ sx: { borderRadius: 3, borderTop: `4px solid ${accent}` } }}>
      <DialogTitle sx={{ pb: 1.5 }}>
        <Box display="flex" alignItems="flex-start" gap={2}>
          <Box flex={1} minWidth={0}>
            <Typography variant="h6" fontWeight={800} color={colors.textPrimary}>
              {finding.display_name}
            </Typography>
            <Box display="flex" gap={0.75} mt={1} flexWrap="wrap">
              <AreaTag area={area} />
              <ImpactChip severity={finding.severity} />
              <AdvisorImpactChip finding={finding} />
            </Box>
          </Box>
          <Box textAlign="right" flexShrink={0}>
            <Typography fontWeight={800} color={SAVINGS_COLOR} sx={{ fontSize: "1.5rem", lineHeight: 1 }}>
              {fmtUSD(finding.estimated_savings_annual)}
            </Typography>
            <Typography variant="caption" color={colors.textMuted}>
              {conditional ? "potential / year" : "per year"}
            </Typography>
          </Box>
          <IconButton onClick={onClose} size="small" sx={{ color: colors.textMuted }}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Box>
      </DialogTitle>

      <DialogContent dividers>
        {/* Business Value */}
        <Section icon={<AutoGraphOutlinedIcon fontSize="small" />} title="Business Value">
          <Box sx={{ p: 2, borderRadius: 2, bgcolor: alpha(accent, 0.07), border: `1px solid ${alpha(accent, 0.22)}` }}>
            <Typography variant="body2" color={colors.textPrimary} fontWeight={500}>{meta.businessValue}</Typography>
          </Box>
        </Section>

        {/* Why This Recommendation Exists */}
        <Section icon={<LightbulbOutlinedIcon fontSize="small" />} title="Why This Recommendation Exists">
          <Typography variant="body2" color={colors.textPrimary}>{finding.description || "—"}</Typography>
        </Section>

        {/* Financial Impact */}
        <Section icon={<PaidOutlinedIcon fontSize="small" />} title="Financial Impact">
          {/* Conditional (AHB): the licence prerequisite is impossible to miss — shown BEFORE the number. */}
          {conditional && (
            <Box
              sx={{
                p: 1.75, mb: 1.5, borderRadius: 2, display: "flex", gap: 1, alignItems: "flex-start",
                bgcolor: alpha(colors.warning, 0.1), border: `1px solid ${alpha(colors.warning, 0.4)}`,
              }}
            >
              <ReportProblemOutlinedIcon sx={{ fontSize: 18, color: colors.warning, mt: "1px" }} />
              <Typography variant="body2" color={colors.textPrimary} fontWeight={500}>
                <b>Requires eligible Windows Server licences.</b> This is a <b>potential</b> saving — you
                only realise it on VMs covered by Windows Server licences you already own with active
                Software Assurance (or qualifying subscription licences). You do <b>not</b> receive this
                amount automatically by enabling Azure Hybrid Benefit.
              </Typography>
            </Box>
          )}
          <Box
            display="flex" gap={{ xs: 3, md: 4 }} flexWrap="wrap" alignItems="flex-end"
            sx={{ p: 2, borderRadius: 2, bgcolor: alpha(SAVINGS_COLOR, 0.06), border: `1px solid ${alpha(SAVINGS_COLOR, 0.22)}` }}
          >
            <FinStat
              label={conditional ? "Potential annual savings" : "Annual savings"}
              value={fmtUSD(finding.estimated_savings_annual)} accent={SAVINGS_COLOR} big
            />
            <FinStat
              label={conditional ? "Potential monthly savings" : "Monthly savings"}
              value={fmtUSD(finding.estimated_savings_monthly)} accent={SAVINGS_COLOR}
            />
            {actual != null && actual > 0 && <FinStat label="Current resource cost" value={`${fmtUSD(actual)} / mo`} />}
            {costReductionPct != null && <FinStat label="Cost reduction" value={fmtPct(costReductionPct)} />}
            <Box flex={1} />
            <Box alignSelf="center"><ValidationChip finding={finding} /></Box>
          </Box>
          {conditional && (eligibleCount != null || excludedCount > 0 || partialBilling) && (
            <Typography variant="caption" color={colors.textMuted} sx={{ display: "block", mt: 1 }}>
              {eligibleCount != null && (
                <>Based on <b>{eligibleCount}</b> VM{eligibleCount === 1 ? "" : "s"} with billed cost and a live licence price</>
              )}
              {excludedCount > 0 && (
                <> · <b>{excludedCount}</b> excluded (no billing or no live licence price)</>
              )}
              {partialBilling && <> · figures reflect a partial billing period — re-run after a full billing month</>}
              .
            </Typography>
          )}
        </Section>

        {/* Affected Resources — count always visible; the list expands on request */}
        <AffectedResourcesSection finding={finding} />

        {/* Secondary detail — collapsed by default so the modal stays scannable, not a wall of text */}
        <CollapsibleSection icon={<BuildOutlinedIcon fontSize="small" />} title="Implementation Guidance">
          <Typography variant="body2" color={colors.textPrimary}>{finding.recommendation || "—"}</Typography>
        </CollapsibleSection>

        {meta.prerequisites && (
          <CollapsibleSection icon={<RuleOutlinedIcon fontSize="small" />} title="Prerequisites">
            <Box sx={{ p: 1.75, borderRadius: 2, bgcolor: alpha(colors.warning, 0.07), border: `1px solid ${alpha(colors.warning, 0.28)}`, display: "flex", gap: 1, alignItems: "flex-start" }}>
              <ReportProblemOutlinedIcon sx={{ fontSize: 17, color: colors.warning, mt: "1px" }} />
              <Typography variant="body2" color={colors.textSecondary}>{meta.prerequisites}</Typography>
            </Box>
          </CollapsibleSection>
        )}

        {showMetrics && (
          <CollapsibleSection icon={<InsightsOutlinedIcon fontSize="small" />} title="Supporting Metrics">
            <FindingEvidence finding={finding} variant="metrics" />
          </CollapsibleSection>
        )}
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 2 }}>
        <Tooltip title="Removes this from the total savings — for recommendations that don't apply to you. It isn't deleted; you can restore it anytime from the Excluded list, and we'll offer an Undo.">
          <Button
            startIcon={<RemoveCircleOutlineIcon sx={{ fontSize: 16 }} />}
            onClick={onExclude}
            sx={{ color: colors.textMuted, textTransform: "none", mr: "auto" }}
          >
            Exclude from savings
          </Button>
        </Tooltip>
        <Button onClick={onClose} variant="contained">Close</Button>
      </DialogActions>
    </Dialog>
  );
}
