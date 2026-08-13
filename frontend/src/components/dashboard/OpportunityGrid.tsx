import React from "react";
import {
  Alert, Box, Button, Collapse, Grid, Snackbar, Typography,
} from "@mui/material";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import RemoveCircleOutlineIcon from "@mui/icons-material/RemoveCircleOutline";
import RestoreIcon from "@mui/icons-material/Restore";
import KeyboardArrowDownIcon from "@mui/icons-material/KeyboardArrowDown";
import { colors } from "../../theme";
import type { Finding } from "../../types";
import { useApi } from "../../services/api";
import OpportunityCard from "./OpportunityCard";
import RecommendationDetails from "./RecommendationDetails";
import { fmtUSD } from "./tokens";

function GroupLabel({ children }: { children: React.ReactNode }) {
  return (
    <Typography
      variant="overline"
      sx={{ fontWeight: 700, letterSpacing: "0.08em", color: colors.textMuted, display: "block", mb: 1.5 }}
    >
      {children}
    </Typography>
  );
}

export default function OpportunityGrid({
  findings,
  excludedFindings,
  assessmentId,
}: {
  findings: Finding[];
  excludedFindings: Finding[];
  assessmentId: number;
}) {
  const [selected, setSelected] = React.useState<Finding | null>(null);
  const [showExcluded, setShowExcluded] = React.useState(false);
  const [undo, setUndo] = React.useState<{ id: number; name: string } | null>(null);

  const api = useApi();
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["assessment", assessmentId] });

  const exclude = useMutation({
    mutationFn: (f: Finding) => api.dismissFinding(assessmentId, f.id),
    onSuccess: (_d, f) => { invalidate(); setSelected(null); setUndo({ id: f.id, name: f.display_name }); },
  });
  const restore = useMutation({
    mutationFn: (id: number) => api.restoreFinding(assessmentId, id),
    onSuccess: () => invalidate(),
  });

  // Cost optimisation is ranked by the size of the saving, not by an "impact" severity — the largest
  // opportunities lead. (Severity stays in the data model; it's just not a dimension we surface here.)
  const filtered = React.useMemo(
    () => [...findings].sort((a, b) => b.estimated_savings_annual - a.estimated_savings_annual),
    [findings]
  );

  const featuredCount = filtered.length >= 4 ? 2 : 0;
  const featured = filtered.slice(0, featuredCount);
  const rest = filtered.slice(featuredCount);

  const excludedTotal = excludedFindings.reduce((s, f) => s + f.estimated_savings_annual, 0);

  return (
    <Box>
      {filtered.length === 0 ? (
        <Typography variant="body2" color="text.secondary" sx={{ py: 5, textAlign: "center" }}>
          No optimization opportunities to show.
        </Typography>
      ) : (
        <>
          {featured.length > 0 && (
            <Box mb={4}>
              <GroupLabel>Featured opportunities</GroupLabel>
              <Grid container spacing={2.5}>
                {featured.map((f) => (
                  <Grid item xs={12} md={6} key={f.id}>
                    <OpportunityCard finding={f} variant="featured" onViewDetails={() => setSelected(f)} />
                  </Grid>
                ))}
              </Grid>
            </Box>
          )}

          {rest.length > 0 && (
            <Box>
              {featured.length > 0 && <GroupLabel>All opportunities</GroupLabel>}
              <Grid container spacing={2}>
                {rest.map((f) => (
                  <Grid item xs={12} sm={6} md={4} key={f.id}>
                    <OpportunityCard finding={f} variant="compact" onViewDetails={() => setSelected(f)} />
                  </Grid>
                ))}
              </Grid>
            </Box>
          )}
        </>
      )}

      {/* Excluded recommendations — reversible, clearly separated, never silently lost. */}
      {excludedFindings.length > 0 && (
        <Box mt={4} sx={{ borderRadius: 2, border: `1px dashed ${colors.border}`, bgcolor: colors.surfaceElevated }}>
          <Box
            role="button"
            onClick={() => setShowExcluded((s) => !s)}
            display="flex" alignItems="center" gap={1.25} sx={{ p: 2, cursor: "pointer" }}
          >
            <RemoveCircleOutlineIcon sx={{ fontSize: 18, color: colors.textMuted }} />
            <Typography variant="body2" fontWeight={700} color={colors.textSecondary}>
              {excludedFindings.length} excluded from savings
            </Typography>
            <Typography variant="caption" color={colors.textMuted}>
              · {fmtUSD(excludedTotal)} / yr not counted in the total
            </Typography>
            <Box flex={1} />
            <KeyboardArrowDownIcon
              sx={{ color: colors.textMuted, transform: showExcluded ? "rotate(180deg)" : "none", transition: "transform .2s ease" }}
            />
          </Box>
          <Collapse in={showExcluded} timeout="auto" unmountOnExit>
            <Box px={2} pb={2} display="flex" flexDirection="column" gap={0.5}>
              {excludedFindings.map((f) => (
                <Box
                  key={f.id}
                  display="flex" alignItems="center" justifyContent="space-between" gap={2}
                  sx={{ px: 1.5, py: 1, borderRadius: 1.5, bgcolor: colors.surface, border: `1px solid ${colors.border}` }}
                >
                  <Box minWidth={0}>
                    <Typography variant="body2" fontWeight={600} color={colors.textPrimary} noWrap>
                      {f.display_name}
                    </Typography>
                    <Typography variant="caption" color={colors.textMuted}>
                      {fmtUSD(f.estimated_savings_annual)} / yr
                    </Typography>
                  </Box>
                  <Button
                    size="small"
                    startIcon={<RestoreIcon sx={{ fontSize: 16 }} />}
                    onClick={() => restore.mutate(f.id)}
                    disabled={restore.isPending}
                    sx={{ textTransform: "none", color: colors.accentBlue, flexShrink: 0 }}
                  >
                    Restore
                  </Button>
                </Box>
              ))}
            </Box>
          </Collapse>
        </Box>
      )}

      {selected && (
        <RecommendationDetails
          finding={selected}
          open={Boolean(selected)}
          onClose={() => setSelected(null)}
          onExclude={() => exclude.mutate(selected)}
        />
      )}

      {/* Undo — so an exclusion is never a one-click, irreversible surprise. */}
      <Snackbar
        open={Boolean(undo)}
        autoHideDuration={7000}
        onClose={() => setUndo(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert
          severity="info"
          variant="filled"
          onClose={() => setUndo(null)}
          action={
            <Button
              color="inherit"
              size="small"
              onClick={() => { if (undo) restore.mutate(undo.id); setUndo(null); }}
            >
              Undo
            </Button>
          }
          sx={{ alignItems: "center" }}
        >
          Excluded “{undo?.name}” — savings total updated.
        </Alert>
      </Snackbar>
    </Box>
  );
}
