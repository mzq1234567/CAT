import React from "react";
import { Box, Card, CardContent, Chip, Grid, Tooltip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import SavingsIcon from "@mui/icons-material/Savings";
import CalendarMonthIcon from "@mui/icons-material/CalendarMonth";
import ChecklistIcon from "@mui/icons-material/Checklist";
import PriorityHighIcon from "@mui/icons-material/PriorityHigh";
import FactCheckIcon from "@mui/icons-material/FactCheck";
import AccountBalanceWalletIcon from "@mui/icons-material/AccountBalanceWallet";
import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import { colors, SEVERITY } from "../../theme";
import type { Assessment } from "../../types";
import { StatTile, StackedBar, LegendItem } from "./primitives";
import { AnimatedValue } from "./charts/AnimatedValue";
import { SavingsProjection } from "./charts/SavingsProjection";
import { SAVINGS_COLOR, SPEND_COLOR, fmtCompact, fmtUSD, fmtPct } from "./tokens";

// Shorthands: animated compact currency / percent nodes.
const money = (v: number) => <AnimatedValue value={v} format={fmtCompact} />;
const pct = (v: number) => <AnimatedValue value={v} format={(n) => fmtPct(n)} />;

/** A headline flow number. Compact on the face (e.g. ₹33K); hovering reveals the exact amount. */
function FlowNumber({
  caption,
  value,
  full,
  color,
}: {
  caption: string;
  value: React.ReactNode;
  full: string;
  color: string;
}) {
  return (
    <Box textAlign="center" minWidth={0}>
      <Typography variant="caption" color="text.secondary" textTransform="uppercase" letterSpacing="0.06em">
        {caption}
      </Typography>
      <Tooltip title={full} arrow placement="top">
        <Typography
          variant="h3"
          fontWeight={800}
          color={color}
          sx={{
            fontVariantNumeric: "tabular-nums",
            lineHeight: 1.15,
            cursor: "help",
            display: "inline-block",
            borderBottom: `1px dotted ${alpha(color, 0.35)}`,
          }}
        >
          {value}
        </Typography>
      </Tooltip>
    </Box>
  );
}

export default function ExecutiveSummary({ assessment }: { assessment: Assessment }) {
  const findings = assessment.findings;
  const highImpact = findings.filter((f) => f.severity === "critical" || f.severity === "high").length;

  const hasSpend =
    assessment.cost_data_available && assessment.current_annual_spend != null;
  const currentAnnual = assessment.current_annual_spend ?? 0;
  const savingsAnnual = assessment.total_savings_annual;
  const optimized = currentAnnual - savingsAnnual;
  const savingsPct = currentAnnual > 0 ? (savingsAnnual / currentAnnual) * 100 : 0;

  // Only tell the "current → optimized → %" story when the numbers reconcile
  // (savings can't legitimately exceed measured spend). Otherwise fall back honestly.
  const reconciles = hasSpend && savingsAnnual <= currentAnnual;
  const savingsExceedSpend = hasSpend && savingsAnnual > currentAnnual;

  // ── Full spend story (real billing data present + reconciles) ─────────────
  if (reconciles) {
    return (
      <Box>
        <Card sx={{ mb: 2.5 }}>
          <CardContent sx={{ p: { xs: 3, md: 4 } }}>
            {/* The three headline numbers live here and nowhere else (no repeated tiles below). */}
            <Box display="flex" alignItems="center" justifyContent="space-between" flexWrap="wrap" gap={3} mb={3}>
              <FlowNumber
                caption="Current annual spend"
                value={money(currentAnnual)}
                full={fmtUSD(currentAnnual)}
                color={colors.textPrimary}
              />
              <Box display="flex" flexDirection="column" alignItems="center" gap={0.5}>
                <ArrowForwardIcon sx={{ color: colors.textMuted }} />
                <Tooltip
                  title={`${fmtUSD(savingsAnnual)} / yr · ${fmtUSD(assessment.total_savings_monthly)} / mo`}
                  arrow
                  placement="top"
                >
                  <Chip
                    label={`− ${fmtCompact(savingsAnnual)} savings`}
                    sx={{
                      bgcolor: alpha(SAVINGS_COLOR, 0.15), color: SAVINGS_COLOR,
                      border: `1px solid ${alpha(SAVINGS_COLOR, 0.3)}`, fontWeight: 700, cursor: "help",
                    }}
                  />
                </Tooltip>
              </Box>
              <FlowNumber
                caption="Optimized annual spend"
                value={money(optimized)}
                full={fmtUSD(optimized)}
                color={SAVINGS_COLOR}
              />
              <Tooltip title={`${fmtPct(savingsPct, 1)} of ${fmtUSD(currentAnnual)}`} arrow placement="top">
                <Box
                  sx={{
                    ml: "auto", textAlign: "center", px: 3, py: 1.5, borderRadius: 3, cursor: "help",
                    background: `linear-gradient(160deg, ${alpha(SAVINGS_COLOR, 0.2)} 0%, ${alpha(SAVINGS_COLOR, 0.05)} 100%)`,
                    border: `1px solid ${alpha(SAVINGS_COLOR, 0.35)}`,
                  }}
                >
                  <Typography variant="h3" fontWeight={800} color={SAVINGS_COLOR} lineHeight={1}>
                    {pct(savingsPct)}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">potential reduction</Typography>
                </Box>
              </Tooltip>
            </Box>

            <StackedBar
              height={44}
              segments={[
                { value: optimized, color: SPEND_COLOR, label: `Optimized spend · ${fmtUSD(optimized)}` },
                { value: savingsAnnual, color: SAVINGS_COLOR, label: `Potential savings · ${fmtUSD(savingsAnnual)}` },
              ]}
            />
            <Box display="flex" gap={3} mt={1.5} flexWrap="wrap" alignItems="center">
              <LegendItem color={SPEND_COLOR} label="Optimized spend" value={fmtUSD(optimized)} />
              <LegendItem color={SAVINGS_COLOR} label="Potential savings" value={fmtUSD(savingsAnnual)} />
              <Box flex={1} />
              <Typography variant="caption" color={colors.textMuted}>
                {assessment.findings_count} opportunit{assessment.findings_count === 1 ? "y" : "ies"}
                {highImpact > 0 && (
                  <> · <Box component="span" sx={{ color: SEVERITY.high.solid, fontWeight: 700 }}>{highImpact} high-impact</Box></>
                )}
              </Typography>
            </Box>
          </CardContent>
        </Card>

        {/* Interactive projection replaces the old repeated stat tiles. */}
        <Card>
          <CardContent sx={{ p: 3 }}>
            <SavingsProjection
              monthlySavings={assessment.total_savings_monthly}
              monthlySpend={assessment.current_monthly_spend}
              annualGrowth={assessment.observed_annual_growth}
            />
          </CardContent>
        </Card>
      </Box>
    );
  }

  // ── Savings-led fallback (no billing access) ──────────────────────────────
  return (
    <Box>
      <Card
        sx={{
          mb: 2.5,
          background: `linear-gradient(160deg, ${alpha(SAVINGS_COLOR, 0.14)} 0%, ${alpha(colors.surface, 0)} 55%)`,
          borderColor: alpha(SAVINGS_COLOR, 0.3),
        }}
      >
        <CardContent sx={{ p: { xs: 3, md: 4 } }}>
          <Box display="flex" alignItems="center" gap={1} mb={1} sx={{ color: SAVINGS_COLOR }}>
            <SavingsIcon fontSize="small" />
            <Typography variant="caption" fontWeight={700} textTransform="uppercase" letterSpacing="0.06em">
              Identified annual savings
            </Typography>
          </Box>
          <Box display="flex" alignItems="baseline" gap={2} flexWrap="wrap">
            <Typography variant="h2" fontWeight={800} color={SAVINGS_COLOR} sx={{ fontVariantNumeric: "tabular-nums", lineHeight: 1 }}>
              {money(savingsAnnual)}
            </Typography>
            <Typography variant="h6" color="text.secondary" fontWeight={500}>
              {fmtUSD(assessment.total_savings_monthly)} / month
            </Typography>
          </Box>
          <Typography variant="body1" color="text.secondary" mt={1.5}>
            {findings.length} optimization {findings.length === 1 ? "opportunity" : "opportunities"} found
            {highImpact > 0 && (
              <>
                {" "}·{" "}
                <Box component="span" sx={{ color: SEVERITY.high.solid, fontWeight: 600 }}>{highImpact} high impact</Box>
              </>
            )}
            {" "}across {assessment.subscription_ids.length} subscription
            {assessment.subscription_ids.length !== 1 ? "s" : ""}.
          </Typography>

          {savingsExceedSpend ? (
            <Box
              display="flex"
              alignItems="flex-start"
              gap={1}
              mt={2.5}
              p={1.5}
              sx={{
                borderRadius: 2,
                bgcolor: alpha(colors.warning, 0.08),
                border: `1px solid ${alpha(colors.warning, 0.3)}`,
              }}
            >
              <InfoOutlinedIcon sx={{ fontSize: 16, color: colors.warning, mt: "2px" }} />
              <Typography variant="caption" color={colors.textSecondary}>
                Identified savings ({fmtCompact(savingsAnnual)}) exceed the measured annual spend
                ({fmtCompact(currentAnnual)}) for this scope, so a "% reduction" isn't shown. This
                usually means the billing data is partial (e.g. a new or short-lived subscription) or
                that estimates sit at list price. Treat the savings figure as an upper bound.
              </Typography>
            </Box>
          ) : (
            <Box display="flex" alignItems="flex-start" gap={1} mt={2.5}>
              <InfoOutlinedIcon sx={{ fontSize: 16, color: colors.textMuted, mt: "2px" }} />
              <Typography variant="caption" color={colors.textMuted}>
                Total current spend and % reduction aren't shown — Azure billing (Cost Management) data
                wasn't returned for this run. That's usually a temporary throttle on the billing API
                (not a permissions problem); re-running the assessment normally resolves it. If it
                persists, confirm the account has Cost Management Reader.
              </Typography>
            </Box>
          )}
        </CardContent>
      </Card>

      <Grid container spacing={2.5}>
        {savingsExceedSpend && (
          <Grid item xs={6} md={3}>
            <StatTile label="Current Annual Spend" value={money(currentAnnual)} sub={`${fmtUSD(assessment.current_monthly_spend ?? 0)} / mo`}
              icon={<AccountBalanceWalletIcon fontSize="small" />} color={SPEND_COLOR} />
          </Grid>
        )}
        <Grid item xs={6} md={3}>
          <StatTile label="Monthly Savings" value={money(assessment.total_savings_monthly)} sub={`${fmtUSD(savingsAnnual)} / year`}
            icon={<CalendarMonthIcon fontSize="small" />} color={SAVINGS_COLOR} />
        </Grid>
        <Grid item xs={6} md={3}>
          <StatTile label="Total Findings" value={<AnimatedValue value={assessment.findings_count} format={(n) => String(Math.round(n))} />} sub="opportunities"
            icon={<ChecklistIcon fontSize="small" />} color={colors.accentBlue} />
        </Grid>
        <Grid item xs={6} md={3}>
          <StatTile label="High Impact" value={<AnimatedValue value={highImpact} format={(n) => String(Math.round(n))} />} sub="critical & high"
            icon={<PriorityHighIcon fontSize="small" />} color={SEVERITY.high.solid} />
        </Grid>
        {!savingsExceedSpend && (
          <Grid item xs={6} md={3}>
            <StatTile label="Needs Review" value={<AnimatedValue value={assessment.needs_review_count} format={(n) => String(Math.round(n))} />} sub="estimate vs actual"
              icon={<FactCheckIcon fontSize="small" />} color={colors.warning} />
          </Grid>
        )}
      </Grid>
    </Box>
  );
}
