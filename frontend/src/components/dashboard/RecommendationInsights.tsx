import React from "react";
import { Box, Card, CardContent, Grid, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { colors } from "../../theme";
import type { Assessment, Finding } from "../../types";
import {
  rollupByArea, realisableFindings, conditionalSavingsAnnual, countedAnnual, isReviewFinding,
} from "./area";
import { Donut } from "./charts/Donut";
import { Waterfall } from "./charts/Waterfall";
import { AREA_ACCENT, SAVINGS_COLOR, fmtCompact } from "./tokens";

function ChartCard({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <Card sx={{ height: "100%" }}>
      <CardContent sx={{ p: 3, height: "100%", display: "flex", flexDirection: "column" }}>
        <Typography variant="subtitle1" fontWeight={700} color={colors.textPrimary}>
          {title}
        </Typography>
        {subtitle && (
          <Typography variant="caption" color={colors.textMuted} mb={1.5}>
            {subtitle}
          </Typography>
        )}
        <Box flex={1} display="flex" flexDirection="column" justifyContent="center" mt={subtitle ? 0 : 1.5}>
          {children}
        </Box>
      </CardContent>
    </Card>
  );
}

function SummaryRow({ label, value, muted }: { label: string; value: number; muted?: boolean }) {
  return (
    <Box display="flex" alignItems="center" justifyContent="space-between" gap={2}>
      <Typography variant="body2" color={muted ? colors.textMuted : colors.textSecondary}>
        {label}
      </Typography>
      <Typography
        variant="body2"
        fontWeight={800}
        color={muted ? colors.textMuted : colors.textPrimary}
        sx={{ fontVariantNumeric: "tabular-nums" }}
      >
        {value}
      </Typography>
    </Box>
  );
}

export default function RecommendationInsights({
  findings,
  assessment,
}: {
  findings: Finding[];
  assessment: Assessment;
}) {
  // Every savings aggregate is built on REALISABLE findings only — conditional AHB is shown separately
  // (as a caption below), never folded into the donut / totals / waterfall.
  const realisable = React.useMemo(() => realisableFindings(findings), [findings]);
  const totalAnnual = realisable.reduce((s, f) => s + countedAnnual(f), 0);
  const ahbPotential = conditionalSavingsAnnual(findings);

  // ── Savings by Category (donut) ──────────────────────────────────────────
  const rollups = React.useMemo(() => rollupByArea(realisable), [realisable]);
  const donutData = rollups
    .filter((r) => r.savings > 0)
    .map((r) => ({ name: r.area, value: r.savings, color: AREA_ACCENT[r.area] }));
  const [active, setActive] = React.useState<number | null>(null);

  // ── Top Opportunities (largest realisable wins) ──────────────────────────
  const top = React.useMemo(
    () => [...realisable].sort((a, b) => b.estimated_savings_annual - a.estimated_savings_annual).slice(0, 5),
    [realisable]
  );
  const topMax = top[0]?.estimated_savings_annual || 1;

  // ── Third panel: waterfall when spend reconciles, else impact distribution ─
  const hasSpend = !!assessment.cost_data_available && assessment.current_annual_spend != null;
  const currentAnnual = assessment.current_annual_spend ?? 0;
  const reconciles = hasSpend && totalAnnual <= currentAnnual;

  // Cost-focused summary (shown when a projected-spend waterfall can't be drawn) — never a severity split.
  const reviewCount = findings.filter(isReviewFinding).length;
  const quantifiedCount = realisable.length;

  return (
    <Grid container spacing={2.5}>
      {/* Savings by Category */}
      <Grid item xs={12} md={4}>
        <ChartCard title="Savings by Category" subtitle="Share of total identified savings">
          {donutData.length === 0 ? (
            <Box textAlign="center">
              <Typography variant="body2" color="text.secondary">No realisable savings to break down.</Typography>
              {ahbPotential > 0 && (
                <Typography variant="caption" color={colors.textMuted} sx={{ display: "block", mt: 1 }}>
                  {fmtCompact(ahbPotential)} / yr potential via Hybrid Benefit (needs licences)
                </Typography>
              )}
            </Box>
          ) : (
            <Box>
              <Box display="flex" justifyContent="center">
                <Donut
                  data={donutData}
                  total={totalAnnual}
                  centerValue={fmtCompact(totalAnnual)}
                  centerLabel="per year"
                  activeIndex={active}
                  onActive={setActive}
                  format={(v) => fmtCompact(v)}
                />
              </Box>
              {/* Compact legend (labels only) — the per-category ₹/% live in the tiles below, so we
                  don't repeat the whole breakdown twice. */}
              <Box mt={1.5} display="flex" justifyContent="center" gap={2} flexWrap="wrap">
                {donutData.map((d, i) => (
                  <Box
                    key={d.name}
                    onMouseEnter={() => setActive(i)}
                    onMouseLeave={() => setActive(null)}
                    display="flex" alignItems="center" gap={0.75}
                    sx={{ cursor: "default", opacity: active != null && active !== i ? 0.45 : 1, transition: "opacity .15s ease" }}
                  >
                    <Box sx={{ width: 9, height: 9, borderRadius: "3px", bgcolor: d.color }} />
                    <Typography variant="caption" color="text.secondary">{d.name}</Typography>
                  </Box>
                ))}
              </Box>
              {ahbPotential > 0 && (
                <Typography
                  variant="caption"
                  color={colors.textMuted}
                  sx={{ display: "block", textAlign: "center", mt: 1.5 }}
                >
                  + {fmtCompact(ahbPotential)} / yr potential via Hybrid Benefit, shown separately (needs licences)
                </Typography>
              )}
            </Box>
          )}
        </ChartCard>
      </Grid>

      {/* Top Opportunities */}
      <Grid item xs={12} md={4}>
        <ChartCard title="Top Opportunities" subtitle="Largest annual savings by recommendation">
          <Box display="flex" flexDirection="column" gap={1.75}>
            {top.map((f) => (
              <Box key={f.id}>
                <Box display="flex" justifyContent="space-between" alignItems="baseline" mb={0.5} gap={1}>
                  <Typography variant="body2" color="text.secondary" noWrap sx={{ minWidth: 0 }}>
                    {f.display_name}
                  </Typography>
                  <Typography variant="body2" fontWeight={700} color={SAVINGS_COLOR} sx={{ whiteSpace: "nowrap" }}>
                    {fmtCompact(f.estimated_savings_annual)}
                  </Typography>
                </Box>
                <Box sx={{ height: 8, borderRadius: 1, bgcolor: alpha(colors.textMuted, 0.12), overflow: "hidden" }}>
                  <Box sx={{ height: "100%", width: `${(f.estimated_savings_annual / topMax) * 100}%`, minWidth: 4, borderRadius: 1, bgcolor: SAVINGS_COLOR, transition: "width .7s cubic-bezier(0.22,1,0.36,1)" }} />
                </Box>
              </Box>
            ))}
          </Box>
        </ChartCard>
      </Grid>

      {/* Waterfall (or impact distribution when spend is partial) */}
      <Grid item xs={12} md={4}>
        {reconciles ? (
          <ChartCard title="Spend After Optimization" subtitle="Current spend minus savings = projected">
            <Waterfall
              current={currentAnnual}
              savings={totalAnnual}
              projected={currentAnnual - totalAnnual}
              format={(n) => fmtCompact(n)}
            />
          </ChartCard>
        ) : (
          <ChartCard title="Optimization Summary" subtitle="Identified opportunities at a glance">
            <Box>
              <Typography
                sx={{
                  fontSize: "2rem", fontWeight: 800, lineHeight: 1, color: SAVINGS_COLOR,
                  letterSpacing: "-0.02em", fontVariantNumeric: "tabular-nums",
                }}
              >
                {fmtCompact(totalAnnual)}
              </Typography>
              <Typography variant="caption" color={colors.textMuted}>
                total potential savings / year
              </Typography>
              <Box mt={2.5} display="flex" flexDirection="column" gap={1.25}>
                <SummaryRow label="Opportunities identified" value={findings.length} />
                <SummaryRow label="Quantified savings" value={quantifiedCount} />
                {reviewCount > 0 && (
                  <SummaryRow label="Need review · not quantified" value={reviewCount} muted />
                )}
              </Box>
              {ahbPotential > 0 && (
                <Typography variant="caption" color={colors.textMuted} sx={{ display: "block", mt: 2, lineHeight: 1.5 }}>
                  + {fmtCompact(ahbPotential)} / yr potential via Hybrid Benefit, shown separately (needs licences)
                </Typography>
              )}
            </Box>
          </ChartCard>
        )}
      </Grid>
    </Grid>
  );
}
