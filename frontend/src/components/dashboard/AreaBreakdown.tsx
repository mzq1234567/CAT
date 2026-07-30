import { Box, Card, CardContent, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import MemoryIcon from "@mui/icons-material/Memory";
import StorageIcon from "@mui/icons-material/Storage";
import StorageRoundedIcon from "@mui/icons-material/StorageRounded";
import LanIcon from "@mui/icons-material/Lan";
import CategoryIcon from "@mui/icons-material/Category";
import CloseIcon from "@mui/icons-material/Close";
import { colors } from "../../theme";
import type { Area, AreaRollup } from "./area";
import { AREA_ACCENT, SAVINGS_COLOR, fmtUSD, fmtCompact, fmtPct } from "./tokens";

const AREA_ICON: Record<Area, React.ReactNode> = {
  Compute: <MemoryIcon fontSize="small" />,
  Storage: <StorageIcon fontSize="small" />,
  Databases: <StorageRoundedIcon fontSize="small" />,
  Network: <LanIcon fontSize="small" />,
  Other: <CategoryIcon fontSize="small" />,
};

export default function AreaBreakdown({
  rollups,
  total,
  selected,
  onSelect,
  spendByArea,
}: {
  rollups: AreaRollup[];
  total: number;
  selected: Area | null;
  onSelect: (a: Area | null) => void;
  /** Actual monthly spend per area, when billing data is available. */
  spendByArea?: Record<string, number> | null;
}) {
  const max = Math.max(...rollups.map((r) => r.savings), 1);

  return (
    <Card>
      <CardContent sx={{ p: { xs: 2, md: 3 } }}>
        {rollups.map((r, i) => {
          const accent = AREA_ACCENT[r.area];
          const share = total ? (r.savings / total) * 100 : 0;
          // Monthly spend from Cost Management → annualized to compare with annual savings.
          const areaMonthlySpend = spendByArea?.[r.area];
          const areaAnnualSpend = areaMonthlySpend != null ? areaMonthlySpend * 12 : null;
          const isSelected = selected === r.area;
          const dimmed = selected !== null && !isSelected;

          return (
            <Box
              key={r.area}
              onClick={() => onSelect(isSelected ? null : r.area)}
              role="button"
              aria-pressed={isSelected}
              sx={{
                py: 1.75,
                px: 1.5,
                mx: -1.5,
                borderRadius: 2,
                cursor: "pointer",
                borderTop: i === 0 ? "none" : `1px solid ${colors.border}`,
                opacity: dimmed ? 0.5 : 1,
                bgcolor: isSelected ? alpha(accent, 0.08) : "transparent",
                transition: "all 0.15s ease",
                "&:hover": { bgcolor: alpha(accent, 0.06) },
              }}
            >
              <Box display="flex" alignItems="center" gap={1.5} mb={1}>
                <Box
                  sx={{
                    bgcolor: alpha(accent, 0.15),
                    border: `1px solid ${alpha(accent, 0.3)}`,
                    borderRadius: 1.5,
                    p: 0.7,
                    display: "flex",
                    color: accent,
                  }}
                >
                  {AREA_ICON[r.area]}
                </Box>
                <Box flex={1} minWidth={0}>
                  <Typography variant="subtitle2" fontWeight={700} color={colors.textPrimary}>
                    {r.area}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {r.count} finding{r.count !== 1 ? "s" : ""}
                    {areaAnnualSpend != null && areaAnnualSpend > 0
                      ? r.savings <= areaAnnualSpend
                        ? ` · ${fmtPct((r.savings / areaAnnualSpend) * 100)} of ${fmtCompact(areaAnnualSpend)} spend`
                        : ` · on ${fmtCompact(areaAnnualSpend)} measured spend`
                      : ` · ${fmtPct(share)} of savings`}
                  </Typography>
                </Box>
                <Box textAlign="right" display="flex" alignItems="center" gap={1}>
                  <Typography
                    variant="subtitle1"
                    fontWeight={800}
                    color={SAVINGS_COLOR}
                    sx={{ fontVariantNumeric: "tabular-nums" }}
                  >
                    {fmtCompact(r.savings)}
                  </Typography>
                  {isSelected && <CloseIcon sx={{ fontSize: 16, color: colors.textMuted }} />}
                </Box>
              </Box>
              {/* Savings magnitude bar (rounded end, share of the largest area) */}
              <Box sx={{ height: 8, borderRadius: 1, bgcolor: alpha(colors.textMuted, 0.12), overflow: "hidden" }}>
                <Box
                  sx={{
                    height: "100%",
                    width: `${(r.savings / max) * 100}%`,
                    minWidth: 4,
                    borderRadius: 1,
                    bgcolor: SAVINGS_COLOR,
                    transition: "width 0.4s ease",
                  }}
                />
              </Box>
            </Box>
          );
        })}

        <Box
          display="flex"
          justifyContent="space-between"
          alignItems="center"
          mt={1.5}
          pt={1.5}
          sx={{ borderTop: `1px solid ${colors.border}` }}
        >
          <Typography variant="body2" color="text.secondary" fontWeight={600}>
            Total identified savings
          </Typography>
          <Typography variant="subtitle1" fontWeight={800} color={SAVINGS_COLOR}>
            {fmtUSD(total)} / yr
          </Typography>
        </Box>
      </CardContent>
    </Card>
  );
}
