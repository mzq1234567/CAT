import React from "react";
import {
  Alert, Box, Button, Card, CardContent, Checkbox, Chip,
  CircularProgress, InputAdornment, Skeleton, TextField, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import { useNavigate } from "react-router-dom";
import { useQuery, useMutation } from "@tanstack/react-query";
import AssessmentIcon from "@mui/icons-material/Assessment";
import SearchIcon from "@mui/icons-material/Search";
import CloudOffIcon from "@mui/icons-material/CloudOff";
import Layout from "../components/Layout";
import { useApi } from "../services/api";
import { colors } from "../theme";

export default function SelectSubscriptions() {
  const api = useApi();
  const navigate = useNavigate();
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [search, setSearch] = React.useState("");

  const { data: subscriptions, isLoading, error } = useQuery({
    queryKey: ["subscriptions"],
    queryFn: () => api.getSubscriptions(),
  });

  const mutation = useMutation({
    mutationFn: (ids: string[]) => api.createAssessment(ids),
    onSuccess: (assessment) => navigate(`/assessments/${assessment.id}`),
  });

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const selectAll = () => setSelected(new Set(subscriptions?.map((s) => s.id) ?? []));
  const deselectAll = () => setSelected(new Set());

  const filtered = React.useMemo(() => {
    if (!subscriptions) return [];
    const q = search.trim().toLowerCase();
    if (!q) return subscriptions;
    return subscriptions.filter(
      (s) =>
        s.display_name.toLowerCase().includes(q) || s.id.toLowerCase().includes(q)
    );
  }, [subscriptions, search]);

  if (error) {
    return (
      <Layout title="Select Subscriptions" subtitle="Choose the Azure subscriptions to assess.">
        <Alert severity="error">
          Failed to load subscriptions. Ensure your token has{" "}
          <strong>Azure Management</strong> access and try again.
        </Alert>
      </Layout>
    );
  }

  const total = subscriptions?.length ?? 0;

  return (
    <Layout
      title="Select Subscriptions"
      subtitle="Choose one or more Azure subscriptions to analyse. The assessment scans all resources using your Reader access."
    >
      <Box maxWidth={820} sx={{ pb: 12 }}>
        {mutation.isError && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {String((mutation.error as Error).message)}
          </Alert>
        )}

        <Card sx={{ mb: 3 }}>
          <CardContent sx={{ p: 3 }}>
            <Box
              display="flex"
              gap={2}
              flexWrap="wrap"
              alignItems="center"
              justifyContent="space-between"
              mb={2.5}
            >
              <TextField
                size="small"
                placeholder="Search subscriptions…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                sx={{ flex: 1, minWidth: 240 }}
                InputProps={{
                  startAdornment: (
                    <InputAdornment position="start">
                      <SearchIcon fontSize="small" sx={{ color: colors.textMuted }} />
                    </InputAdornment>
                  ),
                }}
              />
              <Box display="flex" gap={1}>
                <Button size="small" onClick={selectAll} disabled={!total}>
                  Select all
                </Button>
                <Button size="small" color="inherit" onClick={deselectAll} disabled={selected.size === 0}>
                  Clear
                </Button>
              </Box>
            </Box>

            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
              {total} subscription{total !== 1 ? "s" : ""} available
            </Typography>

            {isLoading && (
              <Box display="flex" flexDirection="column" gap={1.25}>
                {[0, 1, 2, 3].map((i) => (
                  <Skeleton key={i} variant="rounded" height={64} sx={{ bgcolor: colors.surfaceElevated }} />
                ))}
              </Box>
            )}

            {!isLoading && total === 0 && (
              <Box textAlign="center" py={6}>
                <CloudOffIcon sx={{ fontSize: 48, color: colors.textMuted, mb: 1.5 }} />
                <Typography variant="h6" color={colors.textPrimary} gutterBottom>
                  No subscriptions found
                </Typography>
                <Typography variant="body2" color="text.secondary" maxWidth={420} mx="auto">
                  Ensure your account has Reader access to at least one enabled Azure subscription,
                  then refresh.
                </Typography>
              </Box>
            )}

            {!isLoading && total > 0 && filtered.length === 0 && (
              <Box textAlign="center" py={5}>
                <Typography variant="body2" color="text.secondary">
                  No subscriptions match “{search}”.
                </Typography>
              </Box>
            )}

            <Box display="flex" flexDirection="column" gap={1.25}>
              {filtered.map((sub) => {
                const isSel = selected.has(sub.id);
                return (
                  <Box
                    key={sub.id}
                    onClick={() => toggle(sub.id)}
                    sx={{
                      display: "flex",
                      alignItems: "center",
                      p: 1.75,
                      borderRadius: 2,
                      cursor: "pointer",
                      border: `1px solid ${isSel ? colors.accentBlue : colors.border}`,
                      bgcolor: isSel ? alpha(colors.accentBlue, 0.1) : colors.surfaceElevated,
                      transition: "all 0.15s ease",
                      "&:hover": {
                        borderColor: isSel ? colors.accentBlue : "#475569",
                        bgcolor: isSel ? alpha(colors.accentBlue, 0.14) : "#2c3e54",
                      },
                    }}
                  >
                    <Checkbox checked={isSel} size="small" sx={{ p: 0, mr: 1.75 }} />
                    <Box flex={1} minWidth={0}>
                      <Typography variant="body2" fontWeight={600} color={colors.textPrimary} noWrap>
                        {sub.display_name}
                      </Typography>
                      <Typography
                        variant="caption"
                        sx={{ fontFamily: "monospace", color: colors.textMuted }}
                      >
                        {sub.id}
                      </Typography>
                    </Box>
                    <Chip
                      label={sub.state}
                      size="small"
                      sx={{
                        bgcolor: alpha(colors.success, 0.15),
                        color: colors.success,
                        border: `1px solid ${alpha(colors.success, 0.3)}`,
                        textTransform: "capitalize",
                      }}
                    />
                  </Box>
                );
              })}
            </Box>
          </CardContent>
        </Card>
      </Box>

      {/* Sticky action bar */}
      <Box
        sx={{
          position: "fixed",
          bottom: 0,
          left: 240,
          right: 0,
          px: 4,
          py: 2,
          borderTop: `1px solid ${colors.border}`,
          bgcolor: alpha(colors.surface, 0.92),
          backdropFilter: "blur(8px)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 2,
          zIndex: 1100,
        }}
      >
        <Typography variant="body2" color="text.secondary">
          <Box component="span" sx={{ color: colors.textPrimary, fontWeight: 700 }}>
            {selected.size}
          </Box>{" "}
          of {total} selected
        </Typography>
        <Button
          variant="contained"
          size="large"
          startIcon={
            mutation.isPending ? <CircularProgress size={18} color="inherit" /> : <AssessmentIcon />
          }
          disabled={selected.size === 0 || mutation.isPending}
          onClick={() => mutation.mutate(Array.from(selected))}
          sx={{ px: 4, py: 1.25, fontSize: 15 }}
        >
          {mutation.isPending ? "Starting…" : "Run Assessment"}
        </Button>
      </Box>
    </Layout>
  );
}
