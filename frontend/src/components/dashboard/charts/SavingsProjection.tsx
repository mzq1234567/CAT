import { useMemo, useState } from "react";
import { Box, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { colors } from "../../../theme";
import { SAVINGS_COLOR, SPEND_COLOR, fmtCompact, fmtPct, fmtUSD } from "../tokens";

/**
 * Cumulative-savings projection — the on-screen twin of the PDF's "Linear Growth" scenario.
 *
 * Uses the SAME method as the report: spend grows LINEARLY at the environment's own measured annual
 * rate (`annualGrowth`, from the best-fit line through recent monthly spend), so each month's saving
 * scales with the growing spend. With no measured growth (too little history) it's a flat run-rate —
 * exactly the report's Fixed scenario. No compounding, no invented growth; hovering reveals the exact
 * cumulative amount and the Year 1/2/3 markers anchor the reading.
 */
export function SavingsProjection({
  monthlySavings,
  monthlySpend,
  annualGrowth,
}: {
  monthlySavings: number;
  monthlySpend?: number | null;
  annualGrowth?: number | null;
}) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  // Linear growth applied to the monthly run-rate: saving at month k = monthlySavings × (1 + g·k/12).
  const growth = annualGrowth && annualGrowth > 0 ? annualGrowth : 0;

  const data = useMemo(() => {
    const out: { month: number; cumulative: number }[] = [];
    let cumulative = 0;
    for (let m = 0; m <= 36; m++) {
      out.push({ month: m, cumulative: Math.round(cumulative) });
      cumulative += monthlySavings * (1 + (growth * m) / 12); // add month m's saving for the next step
    }
    return out;
  }, [monthlySavings, growth]);

  const active = hoverIdx != null ? data[hoverIdx] : null;
  const shown = active ?? data[36];
  const year1 = data[12].cumulative;
  const year3 = data[36].cumulative;

  return (
    <Box>
      <Box display="flex" alignItems="baseline" justifyContent="space-between" gap={2} flexWrap="wrap" mb={0.5}>
        <Box minWidth={0}>
          <Typography variant="subtitle1" fontWeight={700} color={colors.textPrimary}>
            Projected cumulative savings
          </Typography>
          <Typography variant="caption" color={colors.textMuted}>
            {growth > 0
              ? `Spend trending ${fmtPct(growth * 100)}/yr (measured) — grown linearly`
              : "Flat run-rate (no growth trend measured)"}
          </Typography>
        </Box>
        <Box textAlign="right">
          <Typography variant="caption" color="text.secondary" display="block" lineHeight={1}>
            {active ? `By month ${shown.month}` : "Over 3 years"}
          </Typography>
          <Typography
            component="span"
            title={fmtUSD(shown.cumulative)}
            sx={{ fontWeight: 800, fontSize: 20, color: SAVINGS_COLOR, fontVariantNumeric: "tabular-nums", cursor: "help" }}
          >
            {fmtCompact(shown.cumulative)}
          </Typography>
        </Box>
      </Box>

      <Box sx={{ width: "100%", height: 200 }}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart
            data={data}
            margin={{ top: 8, right: 12, bottom: 4, left: 4 }}
            onMouseMove={(s: any) => setHoverIdx(s?.activeTooltipIndex ?? null)}
            onMouseLeave={() => setHoverIdx(null)}
          >
            <defs>
              <linearGradient id="savingsFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={SAVINGS_COLOR} stopOpacity={0.28} />
                <stop offset="100%" stopColor={SAVINGS_COLOR} stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} stroke={alpha(colors.textMuted, 0.14)} />
            <XAxis
              dataKey="month"
              ticks={[0, 12, 24, 36]}
              tickFormatter={(m) => (m === 0 ? "Now" : `Yr ${m / 12}`)}
              tick={{ fill: colors.textMuted, fontSize: 12 }}
              axisLine={{ stroke: alpha(colors.textMuted, 0.25) }}
              tickLine={false}
            />
            <YAxis
              width={44}
              tickFormatter={(v) => fmtCompact(v)}
              tick={{ fill: colors.textMuted, fontSize: 11 }}
              axisLine={false}
              tickLine={false}
            />
            {[12, 24].map((m) => (
              <ReferenceLine key={m} x={m} stroke={alpha(colors.textMuted, 0.28)} strokeDasharray="3 3" />
            ))}
            <Tooltip
              cursor={{ stroke: SAVINGS_COLOR, strokeWidth: 1, strokeDasharray: "4 3" }}
              content={({ active: a, payload }: any) => {
                if (!a || !payload?.length) return null;
                const p = payload[0].payload;
                return (
                  <Box
                    sx={{
                      px: 1.5, py: 1, borderRadius: 1.5,
                      bgcolor: colors.surfaceElevated,
                      border: `1px solid ${colors.border}`,
                      boxShadow: "0 6px 20px rgba(0,0,0,0.28)",
                    }}
                  >
                    <Typography variant="caption" color="text.secondary" display="block">
                      {p.month === 0 ? "Today" : `After ${p.month} month${p.month === 1 ? "" : "s"}`}
                    </Typography>
                    <Typography variant="body2" fontWeight={800} sx={{ color: SAVINGS_COLOR }}>
                      {fmtUSD(p.cumulative)} saved
                    </Typography>
                    <Typography variant="caption" color={colors.textMuted}>
                      {growth > 0 ? `at ${fmtPct(growth * 100)}/yr spend growth` : `${fmtUSD(monthlySavings)} / mo`}
                    </Typography>
                  </Box>
                );
              }}
            />
            <Area
              type="monotone"
              dataKey="cumulative"
              stroke={SAVINGS_COLOR}
              strokeWidth={2}
              fill="url(#savingsFill)"
              activeDot={{ r: 4, fill: SAVINGS_COLOR, stroke: colors.surface, strokeWidth: 2 }}
              animationDuration={700}
            />
          </AreaChart>
        </ResponsiveContainer>
      </Box>

      <Box display="flex" gap={3} mt={0.5} flexWrap="wrap">
        <Marker color={SAVINGS_COLOR} label="Year 1" value={fmtUSD(year1)} />
        <Marker color={SAVINGS_COLOR} label="Year 3 total" value={fmtUSD(year3)} />
        {monthlySpend && monthlySpend > 0 ? (
          <Marker color={SPEND_COLOR} label="Monthly run-rate saved" value={fmtUSD(monthlySavings)} />
        ) : null}
      </Box>
    </Box>
  );
}

function Marker({ color, label, value }: { color: string; label: string; value: string }) {
  return (
    <Box display="flex" alignItems="center" gap={0.9}>
      <Box sx={{ width: 9, height: 9, borderRadius: "3px", bgcolor: color }} />
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
      <Typography variant="caption" fontWeight={700} color={colors.textPrimary} sx={{ fontVariantNumeric: "tabular-nums" }}>
        {value}
      </Typography>
    </Box>
  );
}
