import React from "react";
import {
  Box, Card, CardContent, Chip, Collapse, IconButton, InputAdornment,
  Stack, Table, TableBody, TableCell, TableContainer, TableHead,
  TablePagination, TableRow, TableSortLabel, TextField, Tooltip, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import KeyboardArrowDownIcon from "@mui/icons-material/KeyboardArrowDown";
import KeyboardArrowUpIcon from "@mui/icons-material/KeyboardArrowUp";
import SearchIcon from "@mui/icons-material/Search";
import { Finding } from "../types";
import { colors, SEVERITY, SeverityKey } from "../theme";

const fmt = (n: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(n);

function SeverityBadge({ severity }: { severity: string }) {
  const sev = SEVERITY[(severity as SeverityKey)] ?? SEVERITY.low;
  return (
    <Chip
      size="small"
      icon={<Box sx={{ width: 7, height: 7, borderRadius: "50%", bgcolor: sev.solid, ml: 0.75 }} />}
      label={severity.toUpperCase()}
      sx={{
        bgcolor: sev.bg,
        color: sev.text,
        border: `1px solid ${sev.border}`,
        "& .MuiChip-icon": { color: sev.solid },
      }}
    />
  );
}

function ExpandRow({ finding }: { finding: Finding }) {
  const [open, setOpen] = React.useState(false);
  return (
    <>
      <TableRow
        hover
        sx={{ "& > *": { borderBottom: "unset" }, cursor: "pointer" }}
        onClick={() => setOpen(!open)}
      >
        <TableCell width={44}>
          <IconButton size="small" sx={{ color: colors.textSecondary }}>
            {open ? <KeyboardArrowUpIcon fontSize="small" /> : <KeyboardArrowDownIcon fontSize="small" />}
          </IconButton>
        </TableCell>
        <TableCell>
          <SeverityBadge severity={finding.severity} />
        </TableCell>
        <TableCell sx={{ maxWidth: 240 }}>
          <Typography variant="body2" fontWeight={600} color={colors.textPrimary}>
            {finding.display_name}
          </Typography>
          <Typography variant="caption" color={colors.textMuted}>
            {finding.category}
          </Typography>
        </TableCell>
        <TableCell sx={{ maxWidth: 180 }}>
          <Tooltip title={finding.resource_name || ""}>
            <Typography variant="body2" noWrap color="text.secondary">
              {finding.resource_name || "—"}
            </Typography>
          </Tooltip>
        </TableCell>
        <TableCell>
          <Typography variant="body2" color="text.secondary">
            {finding.resource_group || "—"}
          </Typography>
        </TableCell>
        <TableCell align="right" sx={{ fontWeight: 700, color: colors.success }}>
          {fmt(finding.estimated_savings_monthly)}
        </TableCell>
        <TableCell align="right" sx={{ fontWeight: 600, color: colors.accentBlue }}>
          {fmt(finding.estimated_savings_annual)}
        </TableCell>
      </TableRow>
      <TableRow>
        <TableCell colSpan={7} sx={{ py: 0, borderBottom: open ? undefined : "none" }}>
          <Collapse in={open} timeout="auto" unmountOnExit>
            <Box
              sx={{
                py: 2,
                px: 3,
                bgcolor: colors.surfaceElevated,
                borderRadius: 2,
                my: 1,
                border: `1px solid ${colors.border}`,
              }}
            >
              <Typography variant="subtitle2" fontWeight={600} color={colors.textPrimary} mb={0.5}>
                Description
              </Typography>
              <Typography variant="body2" color="text.secondary" mb={1.5}>
                {finding.description || "—"}
              </Typography>
              <Typography variant="subtitle2" fontWeight={600} color={colors.textPrimary} mb={0.5}>
                Recommendation
              </Typography>
              <Typography variant="body2" color="text.secondary" mb={1.5}>
                {finding.recommendation || "—"}
              </Typography>
              {finding.resource_id && (
                <>
                  <Typography variant="subtitle2" fontWeight={600} color={colors.textPrimary} mb={0.5}>
                    Resource ID
                  </Typography>
                  <Typography
                    variant="body2"
                    sx={{ wordBreak: "break-all", fontFamily: "monospace", color: colors.textMuted }}
                  >
                    {finding.resource_id}
                  </Typography>
                </>
              )}
            </Box>
          </Collapse>
        </TableCell>
      </TableRow>
    </>
  );
}

type SortField = "display_name" | "severity" | "estimated_savings_monthly" | "estimated_savings_annual";

interface Props {
  findings: Finding[];
}

export default function FindingsTable({ findings }: Props) {
  const [search, setSearch] = React.useState("");
  const [severityFilter, setSeverityFilter] = React.useState("all");
  const [categoryFilter, setCategoryFilter] = React.useState("all");
  const [page, setPage] = React.useState(0);
  const [rowsPerPage, setRowsPerPage] = React.useState(25);
  const [orderBy, setOrderBy] = React.useState<SortField>("estimated_savings_monthly");
  const [order, setOrder] = React.useState<"asc" | "desc">("desc");

  const categories = React.useMemo(
    () => Array.from(new Set(findings.map((f) => f.category))).sort(),
    [findings]
  );

  const handleSort = (field: SortField) => {
    if (orderBy === field) {
      setOrder(order === "asc" ? "desc" : "asc");
    } else {
      setOrderBy(field);
      setOrder("desc");
    }
    setPage(0);
  };

  const filtered = React.useMemo(() => {
    return findings
      .filter((f) => {
        if (severityFilter !== "all" && f.severity !== severityFilter) return false;
        if (categoryFilter !== "all" && f.category !== categoryFilter) return false;
        if (search) {
          const q = search.toLowerCase();
          return (
            f.display_name.toLowerCase().includes(q) ||
            (f.resource_name || "").toLowerCase().includes(q) ||
            (f.resource_group || "").toLowerCase().includes(q)
          );
        }
        return true;
      })
      .sort((a, b) => {
        const mult = order === "asc" ? 1 : -1;
        const av = a[orderBy] ?? 0;
        const bv = b[orderBy] ?? 0;
        if (typeof av === "string") return mult * av.localeCompare(bv as string);
        return mult * ((av as number) - (bv as number));
      });
  }, [findings, severityFilter, categoryFilter, search, order, orderBy]);

  const paginated = filtered.slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage);

  const sevChips: { label: string; value: string }[] = [
    { label: "All", value: "all" },
    { label: "High", value: "high" },
    { label: "Medium", value: "medium" },
    { label: "Low", value: "low" },
  ];

  return (
    <Card>
      <CardContent sx={{ p: 3 }}>
        <Box display="flex" justifyContent="space-between" alignItems="center" mb={2.5} flexWrap="wrap" gap={1.5}>
          <Typography variant="subtitle1" fontWeight={600}>
            All Findings{" "}
            <Box component="span" sx={{ color: colors.textMuted, fontWeight: 500 }}>
              ({filtered.length})
            </Box>
          </Typography>
          <TextField
            size="small"
            placeholder="Search findings…"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(0);
            }}
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

        {/* Severity filter chips */}
        <Stack direction="row" spacing={1} mb={1.5} flexWrap="wrap" useFlexGap>
          {sevChips.map((c) => (
            <Chip
              key={c.value}
              label={c.label}
              size="small"
              onClick={() => {
                setSeverityFilter(c.value);
                setPage(0);
              }}
              variant={severityFilter === c.value ? "filled" : "outlined"}
              color={severityFilter === c.value ? "primary" : "default"}
              sx={{ borderColor: colors.border }}
            />
          ))}
        </Stack>

        {/* Category filter chips */}
        <Stack direction="row" spacing={1} mb={2.5} flexWrap="wrap" useFlexGap>
          <Chip
            label="All categories"
            size="small"
            onClick={() => {
              setCategoryFilter("all");
              setPage(0);
            }}
            variant={categoryFilter === "all" ? "filled" : "outlined"}
            color={categoryFilter === "all" ? "secondary" : "default"}
            sx={{ borderColor: colors.border }}
          />
          {categories.map((c) => (
            <Chip
              key={c}
              label={c}
              size="small"
              onClick={() => {
                setCategoryFilter(c);
                setPage(0);
              }}
              variant={categoryFilter === c ? "filled" : "outlined"}
              color={categoryFilter === c ? "secondary" : "default"}
              sx={{ borderColor: colors.border }}
            />
          ))}
        </Stack>

        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell width={44} />
                <TableCell>
                  <TableSortLabel
                    active={orderBy === "severity"}
                    direction={orderBy === "severity" ? order : "asc"}
                    onClick={() => handleSort("severity")}
                  >
                    Severity
                  </TableSortLabel>
                </TableCell>
                <TableCell>
                  <TableSortLabel
                    active={orderBy === "display_name"}
                    direction={orderBy === "display_name" ? order : "asc"}
                    onClick={() => handleSort("display_name")}
                  >
                    Finding
                  </TableSortLabel>
                </TableCell>
                <TableCell>Resource</TableCell>
                <TableCell>Resource Group</TableCell>
                <TableCell align="right">
                  <TableSortLabel
                    active={orderBy === "estimated_savings_monthly"}
                    direction={orderBy === "estimated_savings_monthly" ? order : "desc"}
                    onClick={() => handleSort("estimated_savings_monthly")}
                  >
                    Monthly
                  </TableSortLabel>
                </TableCell>
                <TableCell align="right">
                  <TableSortLabel
                    active={orderBy === "estimated_savings_annual"}
                    direction={orderBy === "estimated_savings_annual" ? order : "desc"}
                    onClick={() => handleSort("estimated_savings_annual")}
                  >
                    Annual
                  </TableSortLabel>
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {paginated.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} align="center" sx={{ py: 5, color: colors.textMuted }}>
                    No findings match the current filters.
                  </TableCell>
                </TableRow>
              ) : (
                paginated.map((f) => <ExpandRow key={f.id} finding={f} />)
              )}
            </TableBody>
          </Table>
        </TableContainer>

        <TablePagination
          component="div"
          count={filtered.length}
          page={page}
          onPageChange={(_, p) => setPage(p)}
          rowsPerPage={rowsPerPage}
          onRowsPerPageChange={(e) => {
            setRowsPerPage(parseInt(e.target.value));
            setPage(0);
          }}
          rowsPerPageOptions={[10, 25, 50, 100]}
        />
      </CardContent>
    </Card>
  );
}
