import React from "react";
import {
  Box, Button, Chip, Collapse, Divider, Drawer, IconButton, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import CloseIcon from "@mui/icons-material/Close";
import ReportProblemOutlinedIcon from "@mui/icons-material/ReportProblemOutlined";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import RemoveCircleOutlineIcon from "@mui/icons-material/RemoveCircleOutline";
import KeyboardArrowDownIcon from "@mui/icons-material/KeyboardArrowDown";
import { colors } from "../../theme";
import type { Finding } from "../../types";
import { areaForCategory, isConditionalSaving } from "./area";
import { metaFor, affectedResources, affectedLabel } from "./categoryMeta";
import { AREA_ACCENT, SAVINGS_COLOR, fmtUSD } from "./tokens";
import ResourceList from "./ResourceList";
import FindingEvidence, { hasSupportingMetrics } from "./FindingEvidence";

/**
 * Recommendation decision panel — understandable in ~5–10 seconds.
 *
 * The default view is deliberately compact and visual: the number, three at-a-glance stats, two "why"
 * bullets, compact resource chips, one action + any hard requirement. Everything technical (utilisation
 * charts, the full resource list, methodology) lives behind a collapsed "Technical details" section, so
 * a client isn't handed a report inside a drawer. The framework is the same for every recommendation;
 * only the derived content differs. Financial-integrity states are preserved: a REVIEW finding shows
 * "Not quantified", never a fabricated figure.
 */

type Tone = "verified" | "estimate" | "potential" | "warning";
const TONE: Record<Tone, string> = {
  verified: colors.success,
  estimate: colors.accentBlue,
  potential: colors.warning,
  warning: colors.error,
};

function savingsInfo(finding: Finding, d: Record<string, unknown>): { label: string; tone: Tone } {
  if (d.cost_anomaly === true) return { label: "Verify billing", tone: "warning" };
  if (isConditionalSaving(finding.category)) return { label: "Eligibility required", tone: "potential" };
  if (d.cost_is_estimate === true || d.partial_billing === true) return { label: "Estimated", tone: "estimate" };
  if (finding.validation_status === "validated") return { label: "Cost-validated", tone: "verified" };
  if (d.source === "azure_reservation_recommendations") return { label: "Azure-calculated", tone: "verified" };
  return { label: "Live pricing", tone: "estimate" };
}

function Chiplet({ label, tone }: { label: string; tone: Tone }) {
  const c = TONE[tone];
  return (
    <Chip
      size="small"
      label={label}
      sx={{ height: 22, fontSize: 11, fontWeight: 700, bgcolor: alpha(c, 0.12), color: c, border: `1px solid ${alpha(c, 0.3)}` }}
    />
  );
}

function Note({ tone, title, children }: { tone: Tone; title?: string; children: React.ReactNode }) {
  const c = TONE[tone];
  const Icon = tone === "estimate" ? InfoOutlinedIcon : ReportProblemOutlinedIcon;
  return (
    <Box
      sx={{
        mt: 1.5, p: 1.25, borderRadius: 2, display: "flex", gap: 1, alignItems: "flex-start",
        bgcolor: alpha(c, 0.08), border: `1px solid ${alpha(c, 0.28)}`,
      }}
    >
      <Icon sx={{ fontSize: 16, color: c, mt: "1px", flexShrink: 0 }} />
      <Typography variant="caption" color={colors.textSecondary} sx={{ lineHeight: 1.5 }}>
        {title && <Box component="span" sx={{ fontWeight: 700, color: c }}>{title} </Box>}
        {children}
      </Typography>
    </Box>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Box sx={{ px: 3, py: 2 }}>
      <Typography
        variant="overline"
        sx={{ fontWeight: 700, letterSpacing: "0.08em", color: colors.textMuted, display: "block", mb: 1 }}
      >
        {title}
      </Typography>
      {children}
    </Box>
  );
}

/** One at-a-glance stat block. */
function Stat({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <Box
      sx={{
        flex: 1, minWidth: 0, p: 1.5, borderRadius: 2, textAlign: "center",
        bgcolor: colors.surfaceElevated, border: `1px solid ${colors.border}`,
      }}
    >
      <Typography
        sx={{
          fontSize: "1.15rem", fontWeight: 800, lineHeight: 1.15,
          color: accent ? SAVINGS_COLOR : colors.textPrimary,
          fontVariantNumeric: "tabular-nums",
        }}
        noWrap
      >
        {value}
      </Typography>
      <Typography variant="caption" color={colors.textMuted} sx={{ display: "block", mt: 0.25, lineHeight: 1.3 }}>
        {label}
      </Typography>
    </Box>
  );
}

/** Compact resource chips with progressive disclosure ("+N more" → scrollable full list). */
function ResourceChips({ finding }: { finding: Finding }) {
  const { count, items } = affectedResources(finding);
  const [expanded, setExpanded] = React.useState(false);
  if (count === 0) return null;
  const PREVIEW = 5;
  const preview = items.slice(0, PREVIEW);
  const remaining = count - preview.length;

  return (
    <Box>
      <Typography variant="body2" fontWeight={700} color={colors.textPrimary} sx={{ mb: 1 }}>
        {affectedLabel(finding)}
      </Typography>
      <Box display="flex" flexWrap="wrap" gap={0.75}>
        {preview.map((r, i) => (
          <Chip
            key={`${r.name}-${i}`}
            label={r.name}
            size="small"
            sx={{
              maxWidth: 200, height: 24, fontWeight: 600,
              bgcolor: colors.surface, border: `1px solid ${colors.border}`, color: colors.textSecondary,
            }}
          />
        ))}
        {remaining > 0 && !expanded && (
          <Chip
            label={`+${remaining} more`}
            size="small"
            onClick={() => setExpanded(true)}
            sx={{
              height: 24, fontWeight: 700, cursor: "pointer",
              bgcolor: alpha(colors.accentBlue, 0.1), color: colors.accentBlue,
              border: `1px solid ${alpha(colors.accentBlue, 0.3)}`,
            }}
          />
        )}
      </Box>
      <Collapse in={expanded} timeout="auto" unmountOnExit>
        <Box
          sx={{
            mt: 1, maxHeight: 168, overflowY: "auto", display: "flex", flexWrap: "wrap", gap: 0.75,
            p: 1, borderRadius: 1.5, bgcolor: colors.surfaceElevated, border: `1px solid ${colors.border}`,
          }}
        >
          {items.slice(PREVIEW).map((r, i) => (
            <Chip
              key={`more-${r.name}-${i}`}
              label={r.name}
              size="small"
              sx={{ maxWidth: 220, height: 24, fontWeight: 600, bgcolor: colors.surface, border: `1px solid ${colors.border}`, color: colors.textSecondary }}
            />
          ))}
        </Box>
      </Collapse>
    </Box>
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
  onExclude: () => void;
}) {
  const area = areaForCategory(finding.category);
  const accent = AREA_ACCENT[area];
  const meta = metaFor(finding.category);
  const d = (finding.details || {}) as Record<string, unknown>;

  const conditional = isConditionalSaving(finding.category);
  const review = (finding.evidence_state ?? "quantified") === "review";
  const referencePrice = typeof d.reference_monthly_price === "number" ? d.reference_monthly_price : null;
  const conf = savingsInfo(finding, d);
  const { count } = affectedResources(finding);
  const monthlyCost = typeof finding.actual_monthly_cost === "number" && finding.actual_monthly_cost > 0
    ? finding.actual_monthly_cost : null;

  const costAnomaly = d.cost_anomaly === true;
  const supersededByRi = d.overlap_superseded_by_ri === true;
  const mutexReservation =
    typeof d.mutually_exclusive_with_reservation === "string" ? d.mutually_exclusive_with_reservation : null;

  // Recommended action — drop the "Assessment methodology" tail into the technical section.
  const rec = finding.recommendation || "";
  const mIdx = rec.indexOf("Assessment methodology");
  const actionText = (mIdx >= 0 ? rec.slice(0, mIdx) : rec).trim();
  const methodology = mIdx >= 0 ? rec.slice(mIdx).trim() : "";

  // Two concise "why" bullets from the consulting layer — never a wall of prose.
  const whyBullets = [meta.summary(Math.max(count, 1)), meta.businessValue].filter(Boolean);

  const showTech = hasSupportingMetrics(finding) || count > 1 || Boolean(methodology);

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      transitionDuration={240}
      ModalProps={{ keepMounted: false }}
      PaperProps={{
        sx: {
          width: { xs: "100%", sm: 480, md: 560 },
          maxWidth: "100vw",
          borderLeft: `1px solid ${colors.border}`,
          display: "flex",
          flexDirection: "column",
        },
      }}
      slotProps={{ backdrop: { sx: { backgroundColor: alpha(colors.textPrimary, 0.28) } } }}
    >
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <Box sx={{ borderTop: `3px solid ${accent}`, px: 3, pt: 2.25, pb: 2.25, flexShrink: 0 }}>
        <Box display="flex" alignItems="center" justifyContent="space-between" mb={1.25}>
          <Chip
            size="small"
            label={area}
            sx={{ height: 22, fontSize: 11, fontWeight: 700, bgcolor: alpha(accent, 0.12), color: accent, border: `1px solid ${alpha(accent, 0.28)}` }}
          />
          <IconButton onClick={onClose} size="small" sx={{ color: colors.textMuted, mt: -0.5, mr: -0.75 }}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Box>

        <Typography variant="h6" fontWeight={800} color={colors.textPrimary} sx={{ lineHeight: 1.25, mb: 1.25 }}>
          {finding.display_name}
        </Typography>

        {review ? (
          <Box display="flex" alignItems="center" gap={1.25} flexWrap="wrap">
            <Typography fontWeight={800} color={colors.textSecondary} sx={{ fontSize: "1.5rem", lineHeight: 1 }}>
              Not quantified
            </Typography>
            <Chiplet label="Needs review" tone="warning" />
          </Box>
        ) : (
          <Box display="flex" alignItems="baseline" gap={1.25} flexWrap="wrap">
            <Typography fontWeight={800} sx={{ color: SAVINGS_COLOR, fontSize: "2rem", lineHeight: 1, fontVariantNumeric: "tabular-nums" }}>
              {fmtUSD(finding.estimated_savings_annual)}
            </Typography>
            <Typography variant="body2" color={colors.textMuted}>
              {conditional ? "potential savings / year" : "savings / year"}
            </Typography>
            {finding.estimated_savings_monthly > 0 && (
              <Typography variant="body2" color={colors.textSecondary} sx={{ fontWeight: 600 }}>
                · {fmtUSD(finding.estimated_savings_monthly)} / month
              </Typography>
            )}
            <Box flexBasis="100%" height={0} />
            <Box mt={1}>
              <Chiplet label={conf.label} tone={conf.tone} />
            </Box>
          </Box>
        )}
      </Box>

      <Divider />

      {/* ── Scrollable body ────────────────────────────────────────────────── */}
      <Box sx={{ flex: 1, overflowY: "auto" }}>
        {/* At a glance */}
        <Section title="At a glance">
          <Box display="flex" gap={1.25}>
            <Stat label="Affected resources" value={String(count)} />
            {monthlyCost != null ? (
              <Stat label="Current cost / mo" value={fmtUSD(monthlyCost)} />
            ) : referencePrice != null ? (
              <Stat label="Reference / mo" value={fmtUSD(referencePrice)} />
            ) : finding.estimated_savings_monthly > 0 && !review ? (
              <Stat label="Potential / mo" value={fmtUSD(finding.estimated_savings_monthly)} accent />
            ) : (
              <Stat label="Current cost" value="—" />
            )}
            <Stat
              label={conditional ? "Potential / yr" : "Annual saving"}
              value={review ? "—" : fmtUSD(finding.estimated_savings_annual)}
              accent={!review}
            />
          </Box>
        </Section>
        <Divider />

        {/* Why this was identified — bullets, not a paragraph */}
        <Section title="Why this was identified">
          {review ? (
            <Typography variant="body2" color={colors.textSecondary} sx={{ lineHeight: 1.55 }}>
              A real optimisation signal, but we couldn't establish this resource's actual billed cost,
              so it's shown for review rather than a quantified saving.
            </Typography>
          ) : (
            <Box component="ul" sx={{ m: 0, pl: 2.25, display: "flex", flexDirection: "column", gap: 0.75 }}>
              {whyBullets.map((b, i) => (
                <Box component="li" key={i} sx={{ color: colors.textSecondary }}>
                  <Typography variant="body2" component="span" color={colors.textSecondary} sx={{ lineHeight: 1.5 }}>
                    {b}
                  </Typography>
                </Box>
              ))}
            </Box>
          )}
          {costAnomaly && !review && (
            <Note tone="warning" title="Verify billing.">
              Billed cost is far below list price, likely a sponsored/credited subscription or a currency
              mismatch. Confirm before relying on this figure.
            </Note>
          )}
          {supersededByRi && !review && (
            <Note tone="estimate">
              A Reserved Instance already covers this resource's compute, counted there, not added on top.
            </Note>
          )}
          {mutexReservation && !review && (
            <Note tone="estimate">
              Also appears in the {mutexReservation} recommendation, these are alternatives; treat the two
              as an upper bound, not a sum.
            </Note>
          )}
        </Section>
        <Divider />

        {/* Affected resources — compact chips */}
        {count > 0 && (
          <>
            <Section title="Affected resources">
              <ResourceChips finding={finding} />
            </Section>
            <Divider />
          </>
        )}

        {/* Recommended action + any hard requirement */}
        {(actionText || meta.prerequisites) && (
          <>
            <Section title="Recommended action">
              {actionText && (
                <Typography variant="body2" color={colors.textPrimary} sx={{ lineHeight: 1.55 }}>
                  {actionText}
                </Typography>
              )}
              {review ? (
                <Note tone="warning">
                  Confirm whether it's still needed; re-run once billing detail is available to quantify it.
                </Note>
              ) : (
                meta.prerequisites && (
                  <Note tone={conditional ? "potential" : "warning"} title="Requirement.">
                    {meta.prerequisites}
                  </Note>
                )
              )}
            </Section>
            <Divider />
          </>
        )}

        {/* Technical details — collapsed by default */}
        {showTech && <TechnicalDetails finding={finding} methodology={methodology} />}
      </Box>

      {/* ── Footer ─────────────────────────────────────────────────────────── */}
      <Divider />
      <Box sx={{ px: 3, py: 1.75, display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0 }}>
        <Button
          startIcon={<RemoveCircleOutlineIcon sx={{ fontSize: 16 }} />}
          onClick={onExclude}
          sx={{ color: colors.textMuted, textTransform: "none" }}
        >
          Exclude from savings
        </Button>
        <Button variant="contained" onClick={onClose}>Close</Button>
      </Box>
    </Drawer>
  );
}

function TechnicalDetails({ finding, methodology }: { finding: Finding; methodology: string }) {
  const [open, setOpen] = React.useState(false);
  const { count, items } = affectedResources(finding);
  return (
    <Box sx={{ px: 3, py: 1.75 }}>
      <Box
        role="button"
        onClick={() => setOpen((o) => !o)}
        display="flex" alignItems="center" gap={0.75}
        sx={{ cursor: "pointer", color: colors.textSecondary, "&:hover": { color: colors.textPrimary } }}
      >
        <Typography variant="overline" sx={{ fontWeight: 700, letterSpacing: "0.08em", flex: 1 }}>
          Technical details
        </Typography>
        <KeyboardArrowDownIcon sx={{ fontSize: 18, transform: open ? "rotate(180deg)" : "none", transition: "transform .2s ease" }} />
      </Box>
      <Collapse in={open} timeout="auto" unmountOnExit>
        <Box mt={1.5}>
          <FindingEvidence finding={finding} variant="metrics" />
          {count > 1 && (
            <Box mt={1.5}>
              <ResourceList items={items} count={count} showSavings={items.length > 1} />
            </Box>
          )}
          {methodology && (
            <Typography variant="caption" color={colors.textMuted} sx={{ display: "block", mt: 1.5, lineHeight: 1.5 }}>
              {methodology}
            </Typography>
          )}
        </Box>
      </Collapse>
    </Box>
  );
}
