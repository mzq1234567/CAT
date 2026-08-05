import React from "react";
import { Box, Card, CardActionArea, Chip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import DoneAllIcon from "@mui/icons-material/DoneAll";
import { colors } from "../../theme";
import type { Finding } from "../../types";
import { Area, areaForCategory } from "./area";
import { AREA_ICON } from "./areaIcons";
import { affectedResources } from "./categoryMeta";
import { AREA_ACCENT, SAVINGS_COLOR, fmtCompact, fmtUSD, fmtPct } from "./tokens";

interface Rollup {
  area: Area;
  annual: number;
  monthly: number;
  recs: number;
  resources: number;
}

export default function OptimizationCategories({
  findings,
  totalAnnual,
  selected,
  onSelect,
}: {
  findings: Finding[];
  totalAnnual: number;
  selected: Area | null;
  onSelect: (a: Area | null) => void;
}) {
  const rollups: Rollup[] = React.useMemo(() => {
    const map = new Map<Area, Rollup>();
    for (const f of findings) {
      const area = areaForCategory(f.category);
      const cur = map.get(area) ?? { area, annual: 0, monthly: 0, recs: 0, resources: 0 };
      cur.annual += f.estimated_savings_annual;
      cur.monthly += f.estimated_savings_monthly;
      cur.recs += 1;
      cur.resources += affectedResources(f).count;
      map.set(area, cur);
    }
    return [...map.values()].sort((a, b) => b.annual - a.annual);
  }, [findings]);

  const totalResources = rollups.reduce((s, r) => s + r.resources, 0);

  return (
    <Box>
      {/* Reset control — a first-class "All categories" chip so the filter state is always obvious. */}
      <Box display="flex" alignItems="center" gap={1} mb={2} flexWrap="wrap">
        <Chip
          icon={<DoneAllIcon sx={{ fontSize: 16 }} />}
          label={`All categories · ${findings.length}`}
          onClick={() => onSelect(null)}
          variant={selected === null ? "filled" : "outlined"}
          sx={{
            fontWeight: 700,
            ...(selected === null
              ? { bgcolor: colors.textPrimary, color: "#fff", "& .MuiChip-icon": { color: "#fff" } }
              : { borderColor: colors.border, color: colors.textSecondary }),
          }}
        />
        {selected && (
          <Typography variant="caption" color={colors.textMuted}>
            Showing <b style={{ color: colors.textSecondary }}>{selected}</b> — click again or "All
            categories" to reset.
          </Typography>
        )}
      </Box>

      <Box
        display="grid"
        gridTemplateColumns={{ xs: "1fr", sm: "1fr 1fr", md: "repeat(auto-fill, minmax(215px, 1fr))" }}
        gap={2}
      >
        {rollups.map((r) => {
          const accent = AREA_ACCENT[r.area];
          const isSel = selected === r.area;
          const dimmed = selected !== null && !isSel;
          const share = totalAnnual ? (r.annual / totalAnnual) * 100 : 0;

          return (
            <Card
              key={r.area}
              sx={{
                opacity: dimmed ? 0.55 : 1,
                borderColor: isSel ? alpha(accent, 0.55) : colors.border,
                borderWidth: isSel ? 1.5 : 1,
                boxShadow: isSel ? `0 8px 26px ${alpha(accent, 0.18)}` : undefined,
                background: isSel
                  ? `linear-gradient(165deg, ${alpha(accent, 0.1)} 0%, ${alpha(colors.surface, 0)} 62%)`
                  : undefined,
                transition: "opacity .15s ease, box-shadow .2s ease, border-color .2s ease",
              }}
            >
              <CardActionArea
                onClick={() => onSelect(isSel ? null : r.area)}
                sx={{ p: 2.25, height: "100%", display: "block" }}
              >
                {/* Header: icon + name, share pill */}
                <Box display="flex" alignItems="center" gap={1.25} mb={1.75}>
                  <Box
                    sx={{
                      width: 34, height: 34, borderRadius: 1.75, display: "flex",
                      alignItems: "center", justifyContent: "center",
                      bgcolor: alpha(accent, 0.14), color: accent,
                    }}
                  >
                    {AREA_ICON[r.area]}
                  </Box>
                  <Typography variant="subtitle1" fontWeight={700} color={colors.textPrimary} flex={1} noWrap>
                    {r.area}
                  </Typography>
                  <Box
                    sx={{
                      px: 1, py: 0.25, borderRadius: 1, bgcolor: alpha(accent, 0.12),
                      color: accent, fontSize: 12, fontWeight: 700,
                    }}
                  >
                    {fmtPct(share)}
                  </Box>
                </Box>

                {/* Headline savings */}
                <Typography
                  sx={{
                    fontSize: "1.65rem", fontWeight: 800, lineHeight: 1, color: SAVINGS_COLOR,
                    letterSpacing: "-0.02em", fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {fmtCompact(r.annual)}
                </Typography>
                <Typography variant="caption" color={colors.textMuted}>
                  / year · {fmtUSD(r.monthly)} / mo
                </Typography>

                {/* Footer stats */}
                <Box
                  display="flex" gap={2} mt={1.75} pt={1.5}
                  sx={{ borderTop: `1px solid ${colors.border}` }}
                >
                  <Box>
                    <Typography variant="subtitle2" fontWeight={800} color={colors.textPrimary} lineHeight={1}>
                      {r.recs}
                    </Typography>
                    <Typography variant="caption" color={colors.textMuted}>
                      recommendation{r.recs === 1 ? "" : "s"}
                    </Typography>
                  </Box>
                  <Box>
                    <Typography variant="subtitle2" fontWeight={800} color={colors.textPrimary} lineHeight={1}>
                      {r.resources}
                    </Typography>
                    <Typography variant="caption" color={colors.textMuted}>
                      resource{r.resources === 1 ? "" : "s"}
                    </Typography>
                  </Box>
                </Box>
              </CardActionArea>
            </Card>
          );
        })}
      </Box>

      {/* Total strip */}
      <Box
        display="flex" justifyContent="space-between" alignItems="center" mt={2} px={0.5}
      >
        <Typography variant="caption" color={colors.textMuted}>
          {rollups.length} categor{rollups.length === 1 ? "y" : "ies"} · {totalResources} affected resources
        </Typography>
        <Typography variant="body2" fontWeight={700} color={colors.textSecondary}>
          Total identified savings{" "}
          <Box component="span" sx={{ color: SAVINGS_COLOR, fontWeight: 800 }}>{fmtUSD(totalAnnual)} / yr</Box>
        </Typography>
      </Box>
    </Box>
  );
}
