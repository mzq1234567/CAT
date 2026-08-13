import React from "react";
import { Box, Button, Typography } from "@mui/material";
import { colors } from "../../theme";
import { SAVINGS_COLOR, fmtUSD } from "./tokens";
import type { AffectedResource } from "./categoryMeta";

/**
 * A single, reusable resource list used everywhere the detail experience lists affected resources.
 * Compact two-line rows (name + SKU · region), an optional per-resource annual saving on the right,
 * the first few shown by default, the rest revealed in-place behind "View all N" with the list
 * scrolling independently. No nested cards, no per-row borders — just clean, aligned rows.
 */
export default function ResourceList({
  items,
  count,
  showSavings = false,
  initial = 6,
}: {
  items: AffectedResource[];
  count: number;
  showSavings?: boolean;
  initial?: number;
}) {
  const [expanded, setExpanded] = React.useState(false);
  const hasSavings = showSavings && items.some((i) => (i.monthly_savings ?? 0) > 0);
  const shown = expanded ? items : items.slice(0, initial);

  const Row = ({ r, i }: { r: AffectedResource; i: number }) => (
    <Box
      display="flex"
      alignItems="center"
      justifyContent="space-between"
      gap={2}
      sx={{ px: 1.5, py: 1, borderRadius: 1.5, bgcolor: i % 2 ? "transparent" : colors.surfaceElevated }}
    >
      <Box minWidth={0}>
        <Typography variant="body2" fontWeight={600} color={colors.textPrimary} noWrap>
          {r.name}
        </Typography>
        {(r.sku || r.region) && (
          <Typography variant="caption" color={colors.textMuted} sx={{ fontFamily: "monospace" }} noWrap>
            {r.sku}
            {r.sku && r.region ? " · " : ""}
            {r.region}
          </Typography>
        )}
      </Box>
      {hasSavings && (r.monthly_savings ?? 0) > 0 && (
        <Typography variant="body2" fontWeight={700} sx={{ color: SAVINGS_COLOR, whiteSpace: "nowrap" }}>
          {fmtUSD((r.monthly_savings ?? 0) * 12)} / yr
        </Typography>
      )}
    </Box>
  );

  return (
    <Box>
      <Box
        sx={{
          maxHeight: expanded ? 300 : "none",
          overflowY: expanded ? "auto" : "visible",
          display: "flex",
          flexDirection: "column",
          gap: 0.25,
        }}
      >
        {shown.map((r, i) => (
          <Row key={`${r.name}-${i}`} r={r} i={i} />
        ))}
      </Box>
      {!expanded && count > initial && (
        <Button
          size="small"
          onClick={() => setExpanded(true)}
          sx={{ mt: 0.5, textTransform: "none", color: colors.accentBlue, fontWeight: 600, px: 1 }}
        >
          View all {count} resources
        </Button>
      )}
    </Box>
  );
}
