import { useState } from "react";
import { Box, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import MemoryIcon from "@mui/icons-material/Memory";
import ArrowRightAltIcon from "@mui/icons-material/ArrowRightAlt";
import SpeedOutlinedIcon from "@mui/icons-material/SpeedOutlined";
import PaidOutlinedIcon from "@mui/icons-material/PaidOutlined";
import SavingsOutlinedIcon from "@mui/icons-material/SavingsOutlined";
import LayersOutlinedIcon from "@mui/icons-material/LayersOutlined";
import { colors } from "../../theme";
import type { Finding } from "../../types";
import { Meter } from "./charts/Meter";
import { CompareRow } from "./charts/CompareRow";
import { SAVINGS_COLOR, SPEND_COLOR, fmtUSD } from "./tokens";

interface ReservationOption {
  label: string;
  monthly_savings: number;
}
interface ReservationItem {
  name?: string;
  sku?: string;
  region?: string;
  quantity?: number;
  monthly_savings?: number;
  monthly_savings_3yr?: number | null;
}
interface EligibleVm {
  name?: string;
  sku?: string;
  region?: string;
  monthly_savings?: number;
}
interface EvidenceDetails {
  max_cpu?: number;
  avg_cpu?: number;
  peak_memory_used_pct?: number | null;
  memory_verified?: boolean;
  current_sku?: string;
  recommended_sku?: string;
  current_vcpu?: number;
  current_memory_gb?: number;
  recommended_vcpu?: number;
  recommended_memory_gb?: number;
  downsize_ceiling_pct?: number;
  // commitments (RI / Savings Plan)
  payg_monthly?: number;
  ri_price_estimated?: boolean;
  reservation_options?: ReservationOption[];
  // authoritative reservation recs (Azure Consumption engine)
  source?: string;
  monthly_ondemand?: number;
  monthly_reserved?: number;
  recommended_quantity?: number;
  // aggregated commitment finding (many SKUs/VMs rolled into one)
  kind?: string;
  reservation_items?: ReservationItem[];
  total_3yr_monthly?: number | null;
  // aggregated AHB (Windows VMs or SQL resources)
  eligible_vms?: EligibleVm[];
  eligible_count?: number;
  licence_kind?: string; // "Windows Server" (default) | "SQL Server"
}

function Panel({ children }: { children: React.ReactNode }) {
  return (
    <Box
      sx={{
        p: 2,
        mb: 2,
        borderRadius: 2,
        bgcolor: colors.surfaceElevated,
        border: `1px solid ${colors.border}`,
      }}
    >
      {children}
    </Box>
  );
}

function Heading({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <Box display="flex" alignItems="center" gap={0.75} mb={1.5} sx={{ color: colors.textSecondary }}>
      {icon}
      <Typography variant="caption" fontWeight={700} textTransform="uppercase" letterSpacing="0.05em">
        {children}
      </Typography>
    </Box>
  );
}

/**
 * The whole commitment story in ONE panel with a SINGLE term control.
 *
 * Previously this was two panels — a term selector on the options total AND a separate 1yr/3yr
 * toggle on the per-SKU list — which meant two controls doing the same job (one of them looked
 * inert because it only moved the other panel's highlight). Now there is one selector: picking a
 * term highlights that option's total AND re-prices every SKU below it. No redundancy.
 */
function CommitmentPanel({
  options,
  best,
  items,
  kind,
  d,
}: {
  options: ReservationOption[];
  best: ReservationOption | null;
  items: ReservationItem[];
  kind: string;
  d: EvidenceDetails;
}) {
  const sorted = [...options].sort((a, b) => b.monthly_savings - a.monthly_savings);
  const termOf = (label: string): "1yr" | "3yr" => (label.includes("3-year") ? "3yr" : "1yr");
  const hasThreeYrItems = items.some((v) => v.monthly_savings_3yr != null);
  const [selected, setSelected] = useState<string>(best?.label ?? sorted[0]?.label ?? "");

  const selectedTerm: "1yr" | "3yr" = options.length
    ? termOf(selected)
    : hasThreeYrItems
    ? "3yr"
    : "1yr";
  const savingFor = (v: ReservationItem) =>
    selectedTerm === "3yr" ? v.monthly_savings_3yr ?? v.monthly_savings ?? 0 : v.monthly_savings ?? 0;

  return (
    <Panel>
      <Heading icon={<SavingsOutlinedIcon fontSize="small" />}>
        Commitment options{d.ri_price_estimated ? " (RI est.)" : ""}
      </Heading>

      {d.source === "azure_reservation_recommendations" && (
        <Typography variant="caption" color={colors.textMuted} display="block" mb={1}>
          From Azure's own reservation engine — computed on your actual usage at your real prices,
          excluding reservations you already own.
          {d.monthly_ondemand != null && d.monthly_reserved != null && (
            <> On-demand ≈ {fmtUSD(d.monthly_ondemand)}/mo → reserved ≈ {fmtUSD(d.monthly_reserved)}/mo.</>
          )}
        </Typography>
      )}
      {d.source !== "azure_reservation_recommendations" && d.payg_monthly != null && (
        <Typography variant="caption" color={colors.textMuted} display="block" mb={1}>
          Pay-as-you-go today: {fmtUSD(d.payg_monthly)} / mo. Select a term to compare — it re-prices
          the breakdown below.
        </Typography>
      )}

      {/* THE single term control: click a term to select it (highlights + re-prices the list). */}
      {options.length > 0 && (
        <Box display="flex" flexDirection="column" gap={1}>
          {sorted.map((o) => {
            const isSelected = o.label === selected;
            const isBest = o.label === best?.label;
            return (
              <Box
                key={o.label}
                role="button"
                tabIndex={0}
                aria-pressed={isSelected}
                onClick={() => setSelected(o.label)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    setSelected(o.label);
                  }
                }}
                display="flex"
                justifyContent="space-between"
                alignItems="center"
                sx={{
                  px: 1.5,
                  py: 1,
                  borderRadius: 1.5,
                  cursor: "pointer",
                  outline: "none",
                  transition: "background-color 0.15s ease, border-color 0.15s ease",
                  bgcolor: isSelected ? alpha(SAVINGS_COLOR, 0.12) : colors.surface,
                  border: `1.5px solid ${isSelected ? SAVINGS_COLOR : colors.border}`,
                  "&:hover": { borderColor: isSelected ? SAVINGS_COLOR : alpha(SAVINGS_COLOR, 0.5) },
                  "&:focus-visible": { borderColor: SAVINGS_COLOR, boxShadow: `0 0 0 3px ${alpha(SAVINGS_COLOR, 0.2)}` },
                }}
              >
                <Typography variant="body2" fontWeight={isSelected ? 700 : 500} sx={{ color: colors.textPrimary }}>
                  {o.label}
                  {isBest && (
                    <Box
                      component="span"
                      sx={{
                        ml: 1, px: 0.75, py: 0.1, borderRadius: 1, fontSize: 10, fontWeight: 700,
                        color: SAVINGS_COLOR, bgcolor: alpha(SAVINGS_COLOR, 0.15),
                        textTransform: "uppercase", letterSpacing: "0.04em",
                      }}
                    >
                      Best value
                    </Box>
                  )}
                </Typography>
                <Box textAlign="right">
                  <Typography variant="body2" fontWeight={800} sx={{ color: SAVINGS_COLOR, lineHeight: 1.1 }}>
                    {fmtUSD(o.monthly_savings * 12)} / yr
                  </Typography>
                  <Typography variant="caption" color={colors.textMuted}>
                    {fmtUSD(o.monthly_savings)} / mo saved
                  </Typography>
                </Box>
              </Box>
            );
          })}
        </Box>
      )}

      {d.ri_price_estimated && (
        <Typography variant="caption" color={colors.textMuted} mt={1} display="block">
          Reserved Instance price estimated from Azure's typical VM discount (retail reservation
          price wasn't published for this SKU/region). Treat as indicative.
        </Typography>
      )}

      {/* Per-SKU breakdown — reflects the selected term above. No separate toggle. */}
      {items.length > 0 && (
        <>
          <Box display="flex" alignItems="center" gap={0.75} mt={2.5} mb={1} sx={{ color: colors.textSecondary }}>
            <LayersOutlinedIcon fontSize="small" />
            <Typography variant="caption" fontWeight={700} textTransform="uppercase" letterSpacing="0.05em">
              {items.length} {kind}{items.length !== 1 ? "s" : ""} · {selectedTerm === "3yr" ? "3-year" : "1-year"} term
            </Typography>
          </Box>
          <Box display="flex" flexDirection="column" gap={0.5}>
            {items.slice(0, 20).map((v, i) => (
              <Box
                key={`${v.sku}-${v.name}-${i}`}
                display="flex"
                justifyContent="space-between"
                alignItems="baseline"
                sx={{ px: 1.25, py: 0.75, borderRadius: 1, bgcolor: i % 2 ? "transparent" : colors.surface }}
              >
                <Box minWidth={0}>
                  <Typography variant="body2" fontWeight={600} sx={{ color: colors.textPrimary }} noWrap>
                    {v.quantity && v.quantity > 1 ? `${v.quantity}× ` : ""}
                    {v.sku || v.name}
                  </Typography>
                  <Typography variant="caption" color={colors.textMuted}>
                    {v.name && v.name !== v.sku ? v.name : ""}
                    {v.region ? `${v.name && v.name !== v.sku ? " · " : ""}${v.region}` : ""}
                  </Typography>
                </Box>
                <Typography variant="body2" fontWeight={700} sx={{ color: SAVINGS_COLOR, whiteSpace: "nowrap" }}>
                  {fmtUSD(savingFor(v) * 12)} / yr
                </Typography>
              </Box>
            ))}
            {items.length > 20 && (
              <Typography variant="caption" color={colors.textMuted} mt={0.5}>
                +{items.length - 20} more
              </Typography>
            )}
          </Box>
        </>
      )}
    </Panel>
  );
}

export default function FindingEvidence({ finding }: { finding: Finding }) {
  const d = (finding.details || {}) as EvidenceDetails;
  const isVm =
    finding.resource_type === "microsoft.compute/virtualmachines" ||
    finding.category === "idle_vms" ||
    finding.category === "oversized_vms";
  const hasUtil = isVm && d.max_cpu != null;
  const isDownsize = Boolean(d.current_sku && d.recommended_sku && d.current_vcpu != null);
  const ceiling = isDownsize ? d.downsize_ceiling_pct ?? 70 : undefined;
  const ceilingLabel = ceiling != null ? `${ceiling}% safe ceiling` : undefined;
  const hasCostImpact = !isVm && finding.actual_monthly_cost != null && finding.actual_monthly_cost > 0;

  const options = (d.reservation_options || []).filter((o) => o.monthly_savings > 0);
  const hasCommitment = options.length > 0;
  const eligible = (d.eligible_vms || []).filter((v) => (v.monthly_savings ?? 0) > 0);
  const hasAhbList = eligible.length > 0;
  const ahbNoun = finding.category === "sql_ahb" ? "SQL resource" : "VM";
  const ahbLicence = d.licence_kind || "Windows Server";
  const resItems = (d.reservation_items || []).filter((v) => (v.monthly_savings ?? 0) > 0);
  const hasResItems = resItems.length > 0;
  const commitKind = d.kind || "Reserved Instance";
  const bestOption = hasCommitment
    ? options.reduce((a, b) => (b.monthly_savings > a.monthly_savings ? b : a))
    : null;

  if (!hasUtil && !isDownsize && !hasCostImpact && !hasCommitment && !hasAhbList && !hasResItems)
    return null;

  const savePct = hasCostImpact
    ? Math.min(100, (finding.estimated_savings_monthly / (finding.actual_monthly_cost || 1)) * 100)
    : 0;

  return (
    <>
      {/* Downsize headline: current → recommended SKU */}
      {isDownsize && (
        <Box
          sx={{
            p: 2, mb: 2, borderRadius: 2,
            bgcolor: alpha(colors.accentBlue, 0.06),
            border: `1px solid ${alpha(colors.accentBlue, 0.25)}`,
          }}
        >
          <Heading icon={<MemoryIcon fontSize="small" />}>Recommended resize</Heading>
          <Box display="flex" alignItems="center" gap={1} flexWrap="wrap">
            <Typography variant="body1" fontWeight={700} sx={{ fontFamily: "monospace", color: colors.textPrimary }}>
              {d.current_sku}
            </Typography>
            <ArrowRightAltIcon sx={{ color: colors.textMuted }} />
            <Typography variant="body1" fontWeight={700} sx={{ fontFamily: "monospace", color: colors.accentBlue }}>
              {d.recommended_sku}
            </Typography>
          </Box>
        </Box>
      )}

      {/* Utilisation meters — the evidence for idle / oversized */}
      {hasUtil && (
        <Panel>
          <Heading icon={<SpeedOutlinedIcon fontSize="small" />}>30-day peak utilisation</Heading>
          <Box display="flex" flexDirection="column" gap={2}>
            <Meter
              label="Peak CPU"
              value={d.max_cpu ?? null}
              color={SPEND_COLOR}
              threshold={ceiling}
              thresholdLabel={ceilingLabel}
            />
            <Meter
              label="Peak memory used"
              value={d.peak_memory_used_pct ?? null}
              color={colors.accentIndigo}
              threshold={ceiling}
              thresholdLabel={ceilingLabel}
              unavailable={d.memory_verified === false}
            />
          </Box>
        </Panel>
      )}

      {/* Capacity before/after for a downsize */}
      {isDownsize && d.current_vcpu != null && (
        <Panel>
          <Heading icon={<MemoryIcon fontSize="small" />}>Capacity after resize</Heading>
          <Box display="flex" flexDirection="column" gap={2}>
            <CompareRow
              label="vCPUs"
              before={d.current_vcpu}
              after={d.recommended_vcpu ?? 0}
              format={(v) => `${v} vCPU`}
            />
            <CompareRow
              label="Memory"
              before={d.current_memory_gb ?? 0}
              after={d.recommended_memory_gb ?? 0}
              format={(v) => `${v} GB`}
            />
          </Box>
        </Panel>
      )}

      {/* Commitment — one panel, one term control driving both totals and the per-SKU breakdown */}
      {(hasCommitment || hasResItems) && (
        <CommitmentPanel options={options} best={bestOption} items={resItems} kind={commitKind} d={d} />
      )}

      {/* Aggregated AHB — the list of eligible VMs / SQL resources behind the total */}
      {hasAhbList && (
        <Panel>
          <Heading icon={<LayersOutlinedIcon fontSize="small" />}>
            {eligible.length} {ahbNoun}{eligible.length !== 1 ? "s" : ""} eligible for Azure Hybrid Benefit
          </Heading>
          <Box display="flex" flexDirection="column" gap={0.5}>
            {eligible.slice(0, 20).map((v, i) => (
              <Box
                key={`${v.name}-${i}`}
                display="flex"
                justifyContent="space-between"
                alignItems="baseline"
                sx={{
                  px: 1.25, py: 0.75, borderRadius: 1,
                  bgcolor: i % 2 ? "transparent" : colors.surface,
                }}
              >
                <Box minWidth={0}>
                  <Typography variant="body2" fontWeight={600} sx={{ color: colors.textPrimary }} noWrap>
                    {v.name}
                  </Typography>
                  <Typography variant="caption" color={colors.textMuted} sx={{ fontFamily: "monospace" }}>
                    {v.sku}
                    {v.region ? ` · ${v.region}` : ""}
                  </Typography>
                </Box>
                <Typography variant="body2" fontWeight={700} sx={{ color: SAVINGS_COLOR, whiteSpace: "nowrap" }}>
                  {fmtUSD((v.monthly_savings ?? 0) * 12)} / yr
                </Typography>
              </Box>
            ))}
            {eligible.length > 20 && (
              <Typography variant="caption" color={colors.textMuted} mt={0.5}>
                +{eligible.length - 20} more eligible {ahbNoun}s
              </Typography>
            )}
          </Box>
          <Typography variant="caption" color={colors.textMuted} mt={1} display="block">
            Savings assume you hold eligible {ahbLicence} licences with Software Assurance to apply.
          </Typography>
        </Panel>
      )}

      {/* Cost-impact bar for orphaned / idle resources with a known actual cost */}
      {hasCostImpact && (
        <Panel>
          <Heading icon={<PaidOutlinedIcon fontSize="small" />}>Cost impact</Heading>
          <Box display="flex" justifyContent="space-between" alignItems="baseline" mb={0.75}>
            <Typography variant="body2" color="text.secondary">
              Current cost {fmtUSD(finding.actual_monthly_cost || 0)} / mo
            </Typography>
            <Typography variant="body2" fontWeight={800} sx={{ color: SAVINGS_COLOR }}>
              −{fmtUSD(finding.estimated_savings_monthly)} / mo
            </Typography>
          </Box>
          <Box sx={{ height: 12, borderRadius: 6, bgcolor: alpha(colors.textMuted, 0.12), overflow: "hidden" }}>
            <Box
              sx={{
                height: "100%",
                width: `${savePct}%`,
                borderRadius: 6,
                background: `linear-gradient(90deg, ${alpha(SAVINGS_COLOR, 0.85)}, ${SAVINGS_COLOR})`,
              }}
            />
          </Box>
          <Typography variant="caption" color={colors.textMuted} mt={0.5} display="block">
            {Math.round(savePct)}% of this resource's cost eliminated
          </Typography>
        </Panel>
      )}
    </>
  );
}
