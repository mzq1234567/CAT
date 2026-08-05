import React from "react";
import { Box, Card, CardContent, Grid, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { colors, SEVERITY } from "../../theme";
import type { Assessment, Finding, Severity } from "../../types";
import { rollupByArea } from "./area";
import { Donut } from "./charts/Donut";
import { HBars } from "./charts/HBars";
import { Waterfall } from "./charts/Waterfall";
import { AREA_ACCENT, SAVINGS_COLOR, fmtCompact, fmtUSD, fmtPct } from "./tokens";

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

export default function RecommendationInsights({
  findings,
  assessment,
}: {
  findings: Finding[];
  assessment: Assessment;
}) {
  const totalAnnual = findings.reduce((s, f) => s + f.estimated_savings_annual, 0);

  // ── Savings by Category (donut) ──────────────────────────────────────────
  const rollups = React.useMemo(() => rollupByArea(findings), [findings]);
  const donutData = rollups
    .filter((r) => r.savings > 0)
    .map((r) => ({ name: r.area, value: r.savings, color: AREA_ACCENT[r.area] }));
  const [active, setActive] = React.useState<number | null>(null);

  // ── Top Opportunities ────────────────────────────────────────────────────
  const top = React.useMemo(
    () => [...findings].sort((a, b) => b.estimated_savings_annual - a.estimated_savings_annual).slice(0, 5),
    [findings]
  );
  const topMax = top[0]?.estimated_savings_annual || 1;

  // ── Third panel: waterfall when spend reconciles, else impact distribution ─
  const hasSpend = !!assessment.cost_data_available && assessment.current_annual_spend != null;
  const currentAnnual = assessment.current_annual_spend ?? 0;
  const reconciles = hasSpend && totalAnnual <= currentAnnual;

  const impactData = (["critical", "high", "medium", "low"] as Severity[])
    .map((s) => ({ label: SEVERITY[s] ? s[0].toUpperCase() + s.slice(1) : s, value: findings.filter((f) => f.severity === s).length, color: SEVERITY[s].solid }))
    .filter((d) => d.value > 0);

  return (
    <Grid container spacing={2.5}>
      {/* Savings by Category */}
      <Grid item xs={12} md={4}>
        <ChartCard title="Savings by Category" subtitle="Share of total identified savings">
          {donutData.length === 0 ? (
            <Typography variant="body2" color="text.secondary">No savings to break down.</Typography>
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
              <Box mt={1.5} display="flex" flexDirection="column" gap={0.5}>
                {donutData.map((d, i) => (
                  <Box
                    key={d.name}
                    onMouseEnter={() => setActive(i)}
                    onMouseLeave={() => setActive(null)}
                    display="flex" alignItems="center" gap={1}
                    sx={{ px: 1, py: 0.4, mx: -1, borderRadius: 1.5, cursor: "default",
                      bgcolor: active === i ? alpha(d.color, 0.1) : "transparent",
                      opacity: active != null && active !== i ? 0.5 : 1, transition: "all .15s ease" }}
                  >
                    <Box sx={{ width: 9, height: 9, borderRadius: "3px", bgcolor: d.color }} />
                    <Typography variant="body2" color="text.secondary" flex={1}>{d.name}</Typography>
                    <Typography variant="body2" fontWeight={700} color={colors.textPrimary}>{fmtCompact(d.value)}</Typography>
                    <Typography variant="caption" color={colors.textMuted} sx={{ width: 40, textAlign: "right" }}>
                      {fmtPct(totalAnnual ? (d.value / totalAnnual) * 100 : 0)}
                    </Typography>
                  </Box>
                ))}
              </Box>
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
          <ChartCard title="Spend After Optimization" subtitle="Current spend − savings = projected">
            <Waterfall
              current={currentAnnual}
              savings={totalAnnual}
              projected={currentAnnual - totalAnnual}
              format={(n) => fmtCompact(n)}
            />
          </ChartCard>
        ) : (
          <ChartCard title="Recommendations by Impact" subtitle={`${findings.length} opportunities`}>
            {impactData.length === 0 ? (
              <Typography variant="body2" color="text.secondary">No findings.</Typography>
            ) : (
              <HBars data={impactData} />
            )}
          </ChartCard>
        )}
      </Grid>
    </Grid>
  );
}
