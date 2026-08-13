import React from "react";
import { Box, Card, CardContent, Chip, Tooltip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import AccountBalanceWalletIcon from "@mui/icons-material/AccountBalanceWallet";
import SavingsIcon from "@mui/icons-material/Savings";
import TrendingDownIcon from "@mui/icons-material/TrendingDown";
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import ChecklistIcon from "@mui/icons-material/Checklist";
import HelpOutlineIcon from "@mui/icons-material/HelpOutline";
import { colors } from "../../theme";
import type { Assessment } from "../../types";
import { AnimatedValue } from "./charts/AnimatedValue";
import { SAVINGS_COLOR, SPEND_COLOR, fmtCompact, fmtUSD, fmtPct } from "./tokens";
import {
  conditionalSavingsAnnual, realisableFindings, countedAnnual, countedMonthly, isReviewFinding,
} from "./area";

const ahbInfo = (conditionalAnnual: number, fmt: (n: number) => string) =>
  `A further ~${fmt(conditionalAnnual)}/yr is POTENTIALLY available through Azure Hybrid Benefit — ` +
  `conditional on already owning eligible Windows Server licences (with active Software Assurance or a ` +
  `qualifying subscription). It is deliberately NOT included in the savings above, because it isn't ` +
  `automatic: you realise it only on VMs your licences cover. All AHB figures are the licence share of ` +
  `each VM's actual billed cost.`;

/**
 * The 5-second story. Three KPI cards answer, in one glance:
 *   Current Azure Spend  −  Estimated Savings  =  Projected Azure Spend
 * These are the largest elements on the page; everything else is secondary.
 */
function KpiCard({
  label,
  icon,
  accent,
  monthly,
  annual,
  emphasize = false,
  placeholder,
  footer,
  info,
}: {
  label: string;
  icon: React.ReactNode;
  accent: string;
  monthly: number | null;
  annual: number | null;
  emphasize?: boolean;
  placeholder?: string;
  footer?: React.ReactNode;
  info?: string;
}) {
  return (
    <Card
      sx={{
        height: "100%",
        borderColor: emphasize ? alpha(accent, 0.45) : colors.border,
        ...(emphasize && {
          boxShadow: `0 10px 34px ${alpha(accent, 0.2)}`,
          background: `linear-gradient(165deg, ${alpha(accent, 0.11)} 0%, ${alpha(colors.surface, 0)} 60%)`,
        }),
      }}
    >
      <CardContent sx={{ p: { xs: 3, md: 3.5 } }}>
        <Box display="flex" alignItems="center" gap={1.25} mb={2.5}>
          <Box
            sx={{
              width: 38,
              height: 38,
              borderRadius: 2,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              bgcolor: alpha(accent, 0.14),
              color: accent,
            }}
          >
            {icon}
          </Box>
          <Typography
            variant="overline"
            sx={{ fontWeight: 700, letterSpacing: "0.07em", color: colors.textSecondary, lineHeight: 1.25 }}
          >
            {label}
          </Typography>
          {info && (
            <Tooltip title={info} arrow placement="top">
              <InfoOutlinedIcon sx={{ fontSize: 17, color: colors.textMuted, cursor: "help" }} />
            </Tooltip>
          )}
        </Box>

        {monthly != null ? (
          <>
            <Box display="flex" alignItems="baseline" gap={1}>
              <Tooltip title={`${fmtUSD(monthly)} / month`} arrow placement="top">
                <Typography
                  component="div"
                  sx={{
                    fontSize: { xs: "2.2rem", md: "2.8rem" },
                    fontWeight: 800,
                    color: accent,
                    lineHeight: 1,
                    letterSpacing: "-0.02em",
                    fontVariantNumeric: "tabular-nums",
                    cursor: "help",
                  }}
                >
                  <AnimatedValue value={monthly} format={fmtCompact} />
                </Typography>
              </Tooltip>
              <Typography variant="body1" color={colors.textMuted} fontWeight={600}>
                / mo
              </Typography>
            </Box>
            <Tooltip title={annual != null ? `${fmtUSD(annual)} / year` : ""} arrow placement="bottom-start">
              <Typography variant="h6" fontWeight={700} color={colors.textSecondary} mt={1.75} sx={{ cursor: "help", display: "inline-block" }}>
                {annual != null ? fmtCompact(annual) : "—"}{" "}
                <Box component="span" sx={{ fontWeight: 500, color: colors.textMuted, fontSize: "0.78em" }}>
                  / year
                </Box>
              </Typography>
            </Tooltip>
          </>
        ) : (
          <Box py={0.5}>
            <Typography sx={{ fontSize: { xs: "1.35rem", md: "1.6rem" }, fontWeight: 700, color: colors.textMuted, lineHeight: 1.2 }}>
              {placeholder}
            </Typography>
          </Box>
        )}

        {footer && <Box mt={2.25}>{footer}</Box>}
      </CardContent>
    </Card>
  );
}

/** Math operator between cards on desktop; a downward flow arrow when they stack on mobile. */
function Connector({ symbol }: { symbol: "minus" | "equals" }) {
  return (
    <Box
      sx={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexShrink: 0,
        px: { md: 1.25 },
        py: { xs: 0.25, md: 0 },
      }}
    >
      <Typography
        sx={{
          display: { xs: "none", md: "block" },
          fontSize: 30,
          fontWeight: 300,
          color: colors.textMuted,
          lineHeight: 1,
        }}
      >
        {symbol === "minus" ? "−" : "="}
      </Typography>
      <ArrowDownwardIcon sx={{ display: { xs: "block", md: "none" }, color: colors.textMuted, fontSize: 22 }} />
    </Box>
  );
}

function SecondaryStat({
  icon,
  label,
  value,
  color,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  color: string;
}) {
  return (
    <Box display="flex" alignItems="center" gap={1.25}>
      <Box
        sx={{
          width: 32,
          height: 32,
          borderRadius: 1.5,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          bgcolor: alpha(color, 0.12),
          color,
        }}
      >
        {icon}
      </Box>
      <Box>
        <Typography variant="h6" fontWeight={800} lineHeight={1} color={colors.textPrimary}>
          <AnimatedValue value={value} format={(n) => String(Math.round(n))} />
        </Typography>
        <Typography variant="caption" color={colors.textMuted}>
          {label}
        </Typography>
      </Box>
    </Box>
  );
}

export default function ExecutiveSummary({ assessment }: { assessment: Assessment }) {
  const findings = assessment.findings.filter((f) => !f.dismissed);
  // Quantified-vs-review split: REVIEW findings are real signals we couldn't price for this customer —
  // shown as "not quantified" and never in any total. Surfacing the count keeps the split honest.
  const reviewCount = findings.filter(isReviewFinding).length;

  const hasSpend = !!assessment.cost_data_available && assessment.current_annual_spend != null;
  const currentMonthly = assessment.current_monthly_spend ?? null;
  const currentAnnual = assessment.current_annual_spend ?? null;
  // Headline savings = REALISABLE only (conditional AHB is surfaced separately, below). Computed from the
  // live findings so it stays consistent with the donut/category views and reflects exclusions
  // immediately, without depending on a re-persisted backend total.
  const realisable = realisableFindings(findings);
  const savingsMonthly = realisable.reduce((s, f) => s + countedMonthly(f), 0);
  const savingsAnnual = realisable.reduce((s, f) => s + countedAnnual(f), 0);

  // Projected spend = current − savings, but only when the numbers reconcile (savings can't
  // legitimately exceed measured spend; when they do, the billing window is partial).
  const reconciles = hasSpend && currentAnnual != null && savingsAnnual <= currentAnnual;
  const projectedMonthly = reconciles ? (currentMonthly ?? 0) - savingsMonthly : null;
  const projectedAnnual = reconciles ? (currentAnnual ?? 0) - savingsAnnual : null;
  const savingsPct = reconciles && currentAnnual ? (savingsAnnual / currentAnnual) * 100 : null;

  const conditionalAnnual = conditionalSavingsAnnual(findings);
  const ahbInSavings = conditionalAnnual > 0;

  // Spend baseline is an estimated run rate when the subscription has no complete billing month yet.
  // In that case the headline figure is a PROJECTION (average daily × ~30.44), so we also surface the
  // amount ACTUALLY billed so far and over how many days — otherwise "₹64K/mo" reads as money spent when
  // really only ₹31,677 has been billed (over 15 days), which projects to ₹64K/mo.
  const spendEstimated = hasSpend && !!assessment.spend_estimated;
  const spendDays = assessment.spend_period_days;
  const actualBilledSoFar =
    spendEstimated && spendDays && currentMonthly != null
      ? (currentMonthly * spendDays) / 30.4375
      : null;
  const spendInfo = spendEstimated
    ? `This is a PROJECTION, not money already spent.${
        actualBilledSoFar != null && spendDays
          ? ` You've actually been billed ${fmtUSD(actualBilledSoFar)} over ${spendDays} days so far; at that daily rate the month projects to ${fmtUSD(currentMonthly ?? 0)}.`
          : ` The subscription has no complete billing month yet, so spend is its average daily cost normalised to a month.`
      } Re-run after a full billing month for the actual figure.`
    : undefined;
  const spendFooter = spendEstimated ? (
    <Chip
      size="small"
      icon={<InfoOutlinedIcon sx={{ fontSize: 14 }} />}
      label={
        actualBilledSoFar != null && spendDays
          ? `Projected · ${fmtUSD(actualBilledSoFar)} billed in ${spendDays}d`
          : `Estimated run rate${spendDays ? ` · ${spendDays}d billed` : ""}`
      }
      sx={{
        bgcolor: alpha(colors.warning, 0.12), color: colors.warning, fontWeight: 600,
        border: `1px solid ${alpha(colors.warning, 0.3)}`, "& .MuiChip-icon": { color: colors.warning },
      }}
    />
  ) : undefined;

  // Projected card: a clean reduction chip when it reconciles; an honest note otherwise.
  let projectedFooter: React.ReactNode = null;
  if (reconciles && savingsPct != null) {
    projectedFooter = (
      <Chip
        icon={<TrendingDownIcon sx={{ fontSize: 16 }} />}
        label={`${fmtPct(savingsPct)} lower than today`}
        sx={{
          bgcolor: alpha(SAVINGS_COLOR, 0.14),
          color: SAVINGS_COLOR,
          fontWeight: 700,
          border: `1px solid ${alpha(SAVINGS_COLOR, 0.3)}`,
          "& .MuiChip-icon": { color: SAVINGS_COLOR },
        }}
      />
    );
  }

  const caveat = !reconciles
    ? hasSpend
      ? "Estimated savings exceed the measured spend for this scope — the billing data is partial (e.g. a new or recently-migrated subscription), so projected spend isn't shown yet. Treat savings as an upper bound until a full billing month is available."
      : "Azure billing (Cost Management) data wasn't returned for this run, so current and projected spend aren't shown. That's usually a temporary throttle on the billing API; re-running normally resolves it."
    : null;

  return (
    <Box>
      {/* The financial story — the three largest elements on the page. */}
      <Box display="flex" flexDirection={{ xs: "column", md: "row" }} alignItems="stretch" gap={{ xs: 1, md: 0 }}>
        <Box flex={1} minWidth={0}>
          <KpiCard
            label="Current Azure Spend"
            icon={<AccountBalanceWalletIcon />}
            accent={SPEND_COLOR}
            monthly={hasSpend ? currentMonthly : null}
            annual={hasSpend ? currentAnnual : null}
            placeholder="Awaiting billing data"
            info={spendInfo}
            footer={spendFooter}
          />
        </Box>
        <Connector symbol="minus" />
        <Box flex={1} minWidth={0}>
          <KpiCard
            label="Estimated Savings"
            icon={<SavingsIcon />}
            accent={SAVINGS_COLOR}
            emphasize
            monthly={savingsMonthly}
            annual={savingsAnnual}
            footer={
              ahbInSavings ? (
                <Tooltip title={ahbInfo(conditionalAnnual, fmtUSD)} arrow placement="top">
                  <Chip
                    size="small"
                    icon={<InfoOutlinedIcon sx={{ fontSize: 14 }} />}
                    label={`+ ${fmtCompact(conditionalAnnual)} / yr potential · Hybrid Benefit`}
                    sx={{
                      cursor: "help",
                      bgcolor: alpha(colors.warning, 0.12), color: colors.warning, fontWeight: 600,
                      border: `1px solid ${alpha(colors.warning, 0.3)}`,
                      "& .MuiChip-icon": { color: colors.warning },
                    }}
                  />
                </Tooltip>
              ) : undefined
            }
          />
        </Box>
        <Connector symbol="equals" />
        <Box flex={1} minWidth={0}>
          <KpiCard
            label="Projected Azure Spend"
            icon={<TrendingDownIcon />}
            accent={colors.accentBlue}
            monthly={projectedMonthly}
            annual={projectedAnnual}
            placeholder="—"
            footer={projectedFooter}
          />
        </Box>
      </Box>

      {caveat && (
        <Box
          display="flex"
          alignItems="flex-start"
          gap={1}
          mt={2}
          p={1.75}
          sx={{
            borderRadius: 2,
            bgcolor: alpha(colors.warning, 0.07),
            border: `1px solid ${alpha(colors.warning, 0.28)}`,
          }}
        >
          <InfoOutlinedIcon sx={{ fontSize: 18, color: colors.warning, mt: "1px" }} />
          <Typography variant="caption" color={colors.textSecondary}>
            {caveat}
          </Typography>
        </Box>
      )}

      {/* Secondary — opportunity counts. Cost optimisation is framed as opportunities, not severity. */}
      <Box display="flex" gap={{ xs: 3, md: 5 }} mt={2.75} flexWrap="wrap" alignItems="center">
        <SecondaryStat icon={<ChecklistIcon fontSize="small" />} label="Optimization opportunities" value={findings.length} color={colors.accentBlue} />
        {reviewCount > 0 && (
          <Tooltip
            title="Real optimisation signals we could not price for your subscription (e.g. no billed cost available). They're shown as 'not quantified' and are never included in any savings total."
            arrow
            placement="top"
          >
            <Box sx={{ cursor: "help" }}>
              <SecondaryStat
                icon={<HelpOutlineIcon fontSize="small" />}
                label="Need review · not quantified"
                value={reviewCount}
                color={colors.warning}
              />
            </Box>
          </Tooltip>
        )}
      </Box>
    </Box>
  );
}
