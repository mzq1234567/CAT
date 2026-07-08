import { createTheme, alpha } from "@mui/material/styles";

// ---------------------------------------------------------------------------
// Dark Enterprise Theme — Azure CAT
// Palette aligned with slate-900 / slate-800 surfaces and a blue/indigo accent
// system comparable to Datadog, Grafana Cloud and modern Microsoft products.
// ---------------------------------------------------------------------------

export const colors = {
  bg: "#0F172A", // slate-900
  surface: "#1E293B", // slate-800
  surfaceElevated: "#263548",
  border: "#334155", // slate-700
  accentBlue: "#3B82F6",
  accentIndigo: "#6366F1",
  success: "#10B981",
  warning: "#F59E0B",
  error: "#EF4444",
  info: "#06B6D4",
  textPrimary: "#F1F5F9",
  textSecondary: "#94A3B8",
  textMuted: "#64748B",
};

// Severity color tokens — used across findings table, chips, charts.
// The live data model uses high | medium | low; "critical" is supported for
// demo / mock data and forward compatibility.
export const SEVERITY = {
  critical: {
    bg: alpha("#EF4444", 0.15),
    text: "#FCA5A5",
    border: alpha("#EF4444", 0.3),
    solid: "#EF4444",
  },
  high: {
    bg: alpha("#F97316", 0.15),
    text: "#FDB68A",
    border: alpha("#F97316", 0.3),
    solid: "#F97316",
  },
  medium: {
    bg: alpha("#F59E0B", 0.15),
    text: "#FCD34D",
    border: alpha("#F59E0B", 0.3),
    solid: "#F59E0B",
  },
  low: {
    bg: alpha("#06B6D4", 0.15),
    text: "#67E8F9",
    border: alpha("#06B6D4", 0.3),
    solid: "#06B6D4",
  },
} as const;

export type SeverityKey = keyof typeof SEVERITY;

export const theme = createTheme({
  palette: {
    mode: "dark",
    background: { default: colors.bg, paper: colors.surface },
    primary: { main: colors.accentBlue, light: "#60A5FA", dark: "#2563EB" },
    secondary: { main: colors.accentIndigo, light: "#818CF8", dark: "#4F46E5" },
    success: { main: colors.success },
    warning: { main: colors.warning },
    error: { main: colors.error },
    info: { main: colors.info },
    text: {
      primary: colors.textPrimary,
      secondary: colors.textSecondary,
    },
    divider: colors.border,
  },
  typography: {
    fontFamily: '"Inter", "Segoe UI", system-ui, sans-serif',
    h1: { fontWeight: 700, letterSpacing: "-0.5px" },
    h2: { fontWeight: 700, letterSpacing: "-0.5px" },
    h3: { fontWeight: 700, letterSpacing: "-0.4px" },
    h4: { fontWeight: 600, letterSpacing: "-0.3px" },
    h5: { fontWeight: 600, letterSpacing: "-0.2px" },
    h6: { fontWeight: 600 },
    button: { fontWeight: 600 },
  },
  shape: { borderRadius: 10 },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        body: {
          backgroundColor: colors.bg,
          // Subtle radial glow behind the app for depth.
          backgroundImage: `radial-gradient(900px circle at 0% 0%, ${alpha(
            colors.accentIndigo,
            0.08
          )}, transparent 45%), radial-gradient(900px circle at 100% 0%, ${alpha(
            colors.accentBlue,
            0.07
          )}, transparent 45%)`,
          backgroundAttachment: "fixed",
        },
        "*::-webkit-scrollbar": { width: 10, height: 10 },
        "*::-webkit-scrollbar-track": { background: "transparent" },
        "*::-webkit-scrollbar-thumb": {
          background: colors.border,
          borderRadius: 8,
          border: `2px solid ${colors.bg}`,
        },
        "*::-webkit-scrollbar-thumb:hover": { background: "#475569" },
      },
    },
    MuiCard: {
      defaultProps: { elevation: 0 },
      styleOverrides: {
        root: {
          backgroundImage: "none",
          backgroundColor: colors.surface,
          border: `1px solid ${colors.border}`,
          borderRadius: 16,
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: { backgroundImage: "none" },
      },
    },
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: { textTransform: "none", fontWeight: 600, borderRadius: 8 },
        containedPrimary: {
          background: "linear-gradient(135deg, #3B82F6 0%, #6366F1 100%)",
          "&:hover": {
            background: "linear-gradient(135deg, #2563EB 0%, #4F46E5 100%)",
          },
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: { fontWeight: 600, borderRadius: 6 },
      },
    },
    MuiTableCell: {
      styleOverrides: {
        root: { borderColor: colors.border },
        head: {
          color: colors.textSecondary,
          fontWeight: 600,
          fontSize: 12,
          letterSpacing: "0.04em",
          textTransform: "uppercase",
          backgroundColor: alpha(colors.surfaceElevated, 0.5),
        },
      },
    },
    MuiTableRow: {
      styleOverrides: {
        root: {
          "&:hover": { backgroundColor: alpha(colors.accentBlue, 0.06) },
        },
      },
    },
    MuiLinearProgress: {
      styleOverrides: {
        root: { borderRadius: 4, height: 8, backgroundColor: colors.border },
      },
    },
    MuiAlert: {
      styleOverrides: {
        root: { borderRadius: 10, border: `1px solid ${colors.border}` },
      },
    },
    MuiOutlinedInput: {
      styleOverrides: {
        root: {
          backgroundColor: alpha(colors.surfaceElevated, 0.5),
          "& .MuiOutlinedInput-notchedOutline": { borderColor: colors.border },
        },
      },
    },
    MuiTooltip: {
      styleOverrides: {
        tooltip: {
          backgroundColor: colors.surfaceElevated,
          border: `1px solid ${colors.border}`,
          fontSize: 12,
        },
      },
    },
  },
});

// Shared chart helpers for Recharts on the dark surface.
export const chartTheme = {
  grid: colors.border,
  axis: colors.textSecondary,
  tooltipBg: colors.surfaceElevated,
  tooltipBorder: colors.border,
};
