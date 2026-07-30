import React from "react";
import { Box, Chip, InputAdornment, Stack, TextField, Typography } from "@mui/material";
import SearchIcon from "@mui/icons-material/Search";
import { colors } from "../../theme";
import type { Finding, Severity } from "../../types";

const IMPACTS: { label: string; value: "all" | Severity }[] = [
  { label: "All", value: "all" },
  { label: "Critical", value: "critical" },
  { label: "High", value: "high" },
  { label: "Medium", value: "medium" },
  { label: "Low", value: "low" },
];

export default function DetailedFindings({
  findings,
  renderCard,
}: {
  findings: Finding[];
  renderCard: (f: Finding) => React.ReactNode;
}) {
  const [impact, setImpact] = React.useState<"all" | Severity>("all");
  const [search, setSearch] = React.useState("");

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    return findings
      .filter((f) => (impact === "all" ? true : f.severity === impact))
      .filter((f) =>
        q
          ? f.display_name.toLowerCase().includes(q) ||
            (f.resource_name || "").toLowerCase().includes(q) ||
            (f.resource_group || "").toLowerCase().includes(q)
          : true
      )
      .sort((a, b) => b.estimated_savings_annual - a.estimated_savings_annual);
  }, [findings, impact, search]);

  return (
    <Box>
      <Box
        display="flex"
        justifyContent="space-between"
        alignItems="center"
        flexWrap="wrap"
        gap={1.5}
        mb={2}
      >
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
          {IMPACTS.map((c) => (
            <Chip
              key={c.value}
              label={c.label}
              size="small"
              onClick={() => setImpact(c.value)}
              variant={impact === c.value ? "filled" : "outlined"}
              color={impact === c.value ? "primary" : "default"}
              sx={{ borderColor: colors.border }}
            />
          ))}
        </Stack>
        <TextField
          size="small"
          placeholder="Search recommendations…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          sx={{ minWidth: 260 }}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon fontSize="small" sx={{ color: colors.textMuted }} />
              </InputAdornment>
            ),
          }}
        />
      </Box>

      {filtered.length === 0 ? (
        <Typography variant="body2" color="text.secondary" sx={{ py: 4, textAlign: "center" }}>
          No recommendations match the current filters.
        </Typography>
      ) : (
        <Stack spacing={1.5}>{filtered.map((f) => renderCard(f))}</Stack>
      )}
    </Box>
  );
}
