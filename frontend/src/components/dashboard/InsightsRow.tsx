import React from "react";
import { Box, Card, CardContent, Grid, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import type { Finding, Severity } from "../../types";
import { AreaRollup } from "./area";
import { Donut } from "./charts/Donut";
import { HBars } from "./charts/HBars";
import { AREA_ACCENT, fmtCompact, fmtPct } from "./tokens";
import { impactToken, IMPACT_LABEL } from "./tokens";
import { colors } from "../../theme";

const SEVERITY_ORDER: Severity[] = ["critical", "high", "medium", "low"];

export default function InsightsRow({
  rollups,
  totalSavings,
  findings,
}: {
  rollups: AreaRollup[];
  totalSavings: number;
  findings: Finding[];
}) {
  const [active, setActive] = React.useState<number | null>(null);

  const donutData = rollups
    .filter((r) => r.savings > 0)
    .map((r) => ({ name: r.area, value: r.savings, color: AREA_ACCENT[r.area] }));

  const sevCounts = SEVERITY_ORDER.map((s) => ({
    label: IMPACT_LABEL[s],
    value: findings.filter((f) => f.severity === s).length,
    color: impactToken(s).solid,
  })).filter((d) => d.value > 0);

  return (
    <Grid container spacing={2.5}>
      {/* Savings composition */}
      <Grid item xs={12} md={6}>
        <Card sx={{ height: "100%" }}>
          <CardContent sx={{ p: 3 }}>
            <Typography variant="subtitle1" fontWeight={700} mb={2}>
              Savings by area
            </Typography>
            {donutData.length === 0 ? (
              <Typography variant="body2" color="text.secondary">No savings to break down.</Typography>
            ) : (
              <Box display="flex" alignItems="center" gap={3} flexWrap="wrap">
                <Donut
                  data={donutData}
                  total={totalSavings}
                  centerValue={fmtCompact(totalSavings)}
                  centerLabel="per year"
                  activeIndex={active}
                  onActive={setActive}
                  format={(v) => fmtCompact(v)}
                />
                <Box flex={1} minWidth={180} display="flex" flexDirection="column" gap={0.5}>
                  {donutData.map((d, i) => (
                    <Box
                      key={d.name}
                      onMouseEnter={() => setActive(i)}
                      onMouseLeave={() => setActive(null)}
                      display="flex"
                      alignItems="center"
                      gap={1}
                      sx={{
                        px: 1,
                        py: 0.75,
                        mx: -1,
                        borderRadius: 1.5,
                        cursor: "default",
                        bgcolor: active === i ? alpha(d.color, 0.1) : "transparent",
                        opacity: active != null && active !== i ? 0.5 : 1,
                        transition: "all 0.15s ease",
                      }}
                    >
                      <Box sx={{ width: 10, height: 10, borderRadius: "3px", bgcolor: d.color }} />
                      <Typography variant="body2" color="text.secondary" flex={1}>
                        {d.name}
                      </Typography>
                      <Typography variant="body2" fontWeight={700} color={colors.textPrimary}>
                        {fmtCompact(d.value)}
                      </Typography>
                      <Typography variant="caption" color={colors.textMuted} sx={{ width: 42, textAlign: "right" }}>
                        {fmtPct(totalSavings ? (d.value / totalSavings) * 100 : 0)}
                      </Typography>
                    </Box>
                  ))}
                </Box>
              </Box>
            )}
          </CardContent>
        </Card>
      </Grid>

      {/* Impact distribution */}
      <Grid item xs={12} md={6}>
        <Card sx={{ height: "100%" }}>
          <CardContent sx={{ p: 3 }}>
            <Box display="flex" justifyContent="space-between" alignItems="baseline" mb={2}>
              <Typography variant="subtitle1" fontWeight={700}>
                Findings by impact
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {findings.length} total
              </Typography>
            </Box>
            {sevCounts.length === 0 ? (
              <Typography variant="body2" color="text.secondary">No findings.</Typography>
            ) : (
              <Box mt={1}>
                <HBars data={sevCounts} />
              </Box>
            )}
          </CardContent>
        </Card>
      </Grid>
    </Grid>
  );
}
