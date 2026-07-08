import { Box, Card, CardContent, Grid, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import AttachMoneyIcon from "@mui/icons-material/AttachMoney";
import BugReportIcon from "@mui/icons-material/BugReport";
import CategoryIcon from "@mui/icons-material/Category";
import PriorityHighIcon from "@mui/icons-material/PriorityHigh";
import {
  Bar, BarChart, Cell, Pie, PieChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import { Assessment, FindingsByCategory } from "../types";
import { colors, SEVERITY, chartTheme } from "../theme";

interface Props {
  assessment: Assessment;
  byCategory: FindingsByCategory[];
}

const fmt = (n: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);

function StatCard({
  label,
  value,
  sub,
  icon,
  color,
}: {
  label: string;
  value: string;
  sub?: string;
  icon: React.ReactNode;
  color: string;
}) {
  return (
    <Card sx={{ height: "100%" }}>
      <CardContent sx={{ p: 2.5 }}>
        <Box display="flex" alignItems="center" justifyContent="space-between" mb={1.5}>
          <Typography variant="body2" color="text.secondary" fontWeight={500}>
            {label}
          </Typography>
          <Box
            sx={{
              bgcolor: alpha(color, 0.15),
              border: `1px solid ${alpha(color, 0.3)}`,
              borderRadius: 2,
              p: 0.9,
              display: "flex",
              color,
            }}
          >
            {icon}
          </Box>
        </Box>
        <Typography variant="h4" fontWeight={700} color={colors.textPrimary}>
          {value}
        </Typography>
        {sub && (
          <Typography variant="caption" color="text.secondary">
            {sub}
          </Typography>
        )}
      </CardContent>
    </Card>
  );
}

function CustomTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const p = payload[0];
  return (
    <Box
      sx={{
        bgcolor: chartTheme.tooltipBg,
        border: `1px solid ${chartTheme.tooltipBorder}`,
        borderRadius: 1.5,
        px: 1.5,
        py: 1,
      }}
    >
      <Typography variant="caption" color={colors.textPrimary} fontWeight={600} display="block">
        {p.payload.name ?? p.payload.display_name}
      </Typography>
      <Typography variant="caption" color="text.secondary">
        {p.name === "monthly" || p.dataKey === "total_monthly"
          ? `${fmt(p.value)}/mo`
          : `${p.value} finding${p.value !== 1 ? "s" : ""}`}
      </Typography>
    </Box>
  );
}

export default function SummaryCards({ assessment, byCategory }: Props) {
  const findings = assessment.findings;
  const high = findings.filter((f) => f.severity === "high").length;
  const med = findings.filter((f) => f.severity === "medium").length;
  const low = findings.filter((f) => f.severity === "low").length;

  const severityData = [
    { name: "High", key: "high", value: high, color: SEVERITY.high.solid },
    { name: "Medium", key: "medium", value: med, color: SEVERITY.medium.solid },
    { name: "Low", key: "low", value: low, color: SEVERITY.low.solid },
  ].filter((d) => d.value > 0);

  const categoryData = byCategory
    .slice()
    .sort((a, b) => b.total_monthly - a.total_monthly)
    .slice(0, 8);

  return (
    <Box mb={4}>
      {/* Stat cards */}
      <Grid container spacing={2.5} mb={3}>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard
            label="Total Findings"
            value={String(assessment.findings_count)}
            sub="Across all subscriptions"
            icon={<BugReportIcon fontSize="small" />}
            color={colors.accentBlue}
          />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard
            label="High Priority"
            value={String(high)}
            sub={`${med} medium · ${low} low`}
            icon={<PriorityHighIcon fontSize="small" />}
            color={SEVERITY.high.solid}
          />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard
            label="Monthly Savings"
            value={fmt(assessment.total_savings_monthly)}
            sub={`${fmt(assessment.total_savings_annual)} / year`}
            icon={<AttachMoneyIcon fontSize="small" />}
            color={colors.success}
          />
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <StatCard
            label="Categories"
            value={String(byCategory.length)}
            sub="Distinct finding types"
            icon={<CategoryIcon fontSize="small" />}
            color={colors.accentIndigo}
          />
        </Grid>
      </Grid>

      {/* Severity breakdown chips */}
      <Grid container spacing={2.5} mb={3}>
        {(
          [
            { label: "High", count: high, sev: SEVERITY.high },
            { label: "Medium", count: med, sev: SEVERITY.medium },
            { label: "Low", count: low, sev: SEVERITY.low },
          ] as const
        ).map((s) => (
          <Grid item xs={12} sm={4} key={s.label}>
            <Card>
              <CardContent
                sx={{ p: 2, display: "flex", alignItems: "center", gap: 1.5, "&:last-child": { pb: 2 } }}
              >
                <Box sx={{ width: 10, height: 10, borderRadius: "50%", bgcolor: s.sev.solid }} />
                <Typography variant="body2" color="text.secondary" flex={1}>
                  {s.label}
                </Typography>
                <Typography variant="h6" fontWeight={700} color={s.sev.text}>
                  {s.count}
                </Typography>
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      {/* Charts */}
      <Grid container spacing={2.5}>
        <Grid item xs={12} md={5}>
          <Card sx={{ height: "100%" }}>
            <CardContent sx={{ p: 3 }}>
              <Typography variant="subtitle1" fontWeight={600} mb={1}>
                Findings by Severity
              </Typography>
              {severityData.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  No findings.
                </Typography>
              ) : (
                <Box sx={{ height: 260, position: "relative" }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={severityData}
                        dataKey="value"
                        nameKey="name"
                        innerRadius={60}
                        outerRadius={95}
                        paddingAngle={3}
                        stroke="none"
                      >
                        {severityData.map((d) => (
                          <Cell key={d.key} fill={d.color} />
                        ))}
                      </Pie>
                      <Tooltip content={<CustomTooltip />} />
                    </PieChart>
                  </ResponsiveContainer>
                  <Box
                    sx={{
                      position: "absolute",
                      top: "50%",
                      left: "50%",
                      transform: "translate(-50%, -50%)",
                      textAlign: "center",
                      pointerEvents: "none",
                    }}
                  >
                    <Typography variant="h4" fontWeight={700} color={colors.textPrimary}>
                      {assessment.findings_count}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      findings
                    </Typography>
                  </Box>
                </Box>
              )}
              <Box display="flex" justifyContent="center" gap={2} flexWrap="wrap" mt={1}>
                {severityData.map((d) => (
                  <Box key={d.key} display="flex" alignItems="center" gap={0.75}>
                    <Box sx={{ width: 10, height: 10, borderRadius: "50%", bgcolor: d.color }} />
                    <Typography variant="caption" color="text.secondary">
                      {d.name} ({d.value})
                    </Typography>
                  </Box>
                ))}
              </Box>
            </CardContent>
          </Card>
        </Grid>

        <Grid item xs={12} md={7}>
          <Card sx={{ height: "100%" }}>
            <CardContent sx={{ p: 3 }}>
              <Typography variant="subtitle1" fontWeight={600} mb={1}>
                Top Savings by Category
              </Typography>
              {categoryData.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  No category data.
                </Typography>
              ) : (
                <Box sx={{ height: 300 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart
                      data={categoryData}
                      layout="vertical"
                      margin={{ left: 8, right: 16, top: 8, bottom: 8 }}
                    >
                      <XAxis
                        type="number"
                        stroke={chartTheme.axis}
                        tick={{ fill: chartTheme.axis, fontSize: 11 }}
                        tickFormatter={(v) => fmt(v)}
                      />
                      <YAxis
                        type="category"
                        dataKey="display_name"
                        width={130}
                        stroke={chartTheme.axis}
                        tick={{ fill: chartTheme.axis, fontSize: 11 }}
                      />
                      <Tooltip content={<CustomTooltip />} cursor={{ fill: alpha(colors.accentBlue, 0.08) }} />
                      <Bar dataKey="total_monthly" radius={[0, 6, 6, 0]} barSize={18}>
                        {categoryData.map((_, i) => (
                          <Cell
                            key={i}
                            fill={i % 2 === 0 ? colors.accentBlue : colors.accentIndigo}
                          />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </Box>
              )}
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </Box>
  );
}
