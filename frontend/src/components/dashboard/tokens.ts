/**
 * Shared formatting + visual tokens for the executive dashboard.
 *
 * Story color: savings = green (colors.success) — the one number that matters, kept green
 * everywhere. Area hues are labeled accent tags only, never used to encode data alone.
 */
import { colors, SEVERITY } from "../../theme";
import type { Severity } from "../../types";
import type { Area } from "./area";

export const SAVINGS_COLOR = colors.success;
// Spend stays a distinct blue (not the teal brand) so spend-vs-savings reads clearly on charts.
export const SPEND_COLOR = "#3B82F6";

export const AREA_ACCENT: Record<Area, string> = {
  Compute: colors.accentBlue,
  Storage: colors.accentIndigo,
  Databases: colors.info,
  Network: "#A855F7",
  Other: colors.textMuted,
};

// The assessment's billing currency (from Cost Management). Set once per dashboard render via
// setReportCurrency() so every formatter below renders in the client's real currency without
// threading the code through every chart component.
let CURRENCY = "USD";
let SYMBOL = "$";

function currencySymbol(code: string): string {
  try {
    const parts = new Intl.NumberFormat("en", { style: "currency", currency: code }).formatToParts(0);
    return parts.find((p) => p.type === "currency")?.value || "$";
  } catch {
    return "$";
  }
}

export function setReportCurrency(code?: string | null): void {
  CURRENCY = (code || "USD").toUpperCase();
  SYMBOL = currencySymbol(CURRENCY);
}

/** Full currency, no cents, in the active currency: $1,284,000 · ₹1,284,000 · £1,284,000 */
export function fmtUSD(n: number): string {
  try {
    return new Intl.NumberFormat("en", {
      style: "currency", currency: CURRENCY, maximumFractionDigits: 0,
    }).format(n);
  } catch {
    return `${SYMBOL}${Math.round(n).toLocaleString()}`;
  }
}

/** Compact currency for headline numbers in the active currency: $1.28M, ₹312K, £980 */
export function fmtCompact(n: number): string {
  if (Math.abs(n) >= 1_000_000) return `${SYMBOL}${(n / 1_000_000).toFixed(2)}M`;
  if (Math.abs(n) >= 10_000) return `${SYMBOL}${(n / 1_000).toFixed(0)}K`;
  if (Math.abs(n) >= 1_000) return `${SYMBOL}${(n / 1_000).toFixed(1)}K`;
  return `${SYMBOL}${Math.round(n).toLocaleString()}`;
}

export function fmtPct(n: number, digits = 0): string {
  return `${n.toFixed(digits)}%`;
}

export function impactToken(severity: Severity) {
  return SEVERITY[severity];
}

export const IMPACT_LABEL: Record<Severity, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};
