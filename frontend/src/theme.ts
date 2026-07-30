import { createTheme, alpha } from "@mui/material/styles";

// ---------------------------------------------------------------------------
// Light Enterprise Theme — Azure CAT (turbo360-inspired)
// Clean white surfaces on a soft blue-grey page, a cyan/teal brand accent with
// subtle blue→teal gradients, deep-navy text, soft shadows, generous whitespace.
// ---------------------------------------------------------------------------

export const colors = {
  bg: "#F4F7FB", // page background (soft blue-grey)
  surface: "#FFFFFF", // cards / panels
  surfaceElevated: "#F2F6FA", // inset / hover fills
  border: "#E6EBF2", // hairline borders
  accentBlue: "#0AA6BA", // BRAND teal-cyan (primary, nav, buttons)
  accentIndigo: "#5B7CFA", // secondary blue-violet accent
  success: "#12B886", // savings green
  warning: "#F59E0B",
  error: "#EF4444",
  info: "#0EA5E9", // sky blue
  textPrimary: "#0F1B2E", // deep navy (headings / emphasis)
  textSecondary: "#586A85", // slate
  textMuted: "#9AA7BA", // muted labels
};

// Brand gradient — the single source for the teal→cyan brand fill.
export const gradients = {
  brand: "linear-gradient(135deg, #16C8DA 0%, #0A97B6 100%)",
  brandHover: "linear-gradient(135deg, #12B7C8 0%, #08869F 100%)",
};

// Severity tokens tuned for a LIGHT surface: tinted fill + dark, readable text.
export const SEVERITY = {
  critical: { bg: "#FDECEC", text: "#B42318", border: "#F7D3CF", solid: "#E5484D" },
  high: { bg: "#FFF2E8", text: "#B54708", border: "#FBDBC1", solid: "#F7910A" },
  medium: { bg: "#FEF6E6", text: "#8A5A00", border: "#FBE6B2", solid: "#F5B301" },
  low: { bg: "#E7F6F9", text: "#0E7490", border: "#C2E9EF", solid: "#0BB8C4" },
} as const;

export type SeverityKey = keyof typeof SEVERITY;

export const theme = createTheme({
  palette: {
    mode: "light",
    background: { default: colors.bg, paper: colors.surface },
    primary: { main: colors.accentBlue, light: "#3FC6D6", dark: "#0A97B6", contrastText: "#FFFFFF" },
    secondary: { main: colors.accentIndigo, light: "#8AA0FB", dark: "#3F5BD0" },
    success: { main: colors.success },
    warning: { main: colors.warning },
    error: { main: colors.error },
    info: { main: colors.info },
    text: { primary: colors.textPrimary, secondary: colors.textSecondary },
    divider: colors.border,
  },
  typography: {
    fontFamily: '"Inter", "Segoe UI", system-ui, sans-serif',
    h1: { fontWeight: 800, letterSpacing: "-0.6px", color: colors.textPrimary },
    h2: { fontWeight: 800, letterSpacing: "-0.5px", color: colors.textPrimary },
    h3: { fontWeight: 800, letterSpacing: "-0.5px", color: colors.textPrimary },
    h4: { fontWeight: 700, letterSpacing: "-0.4px", color: colors.textPrimary },
    h5: { fontWeight: 700, letterSpacing: "-0.3px", color: colors.textPrimary },
    h6: { fontWeight: 700, color: colors.textPrimary },
    subtitle1: { fontWeight: 600 },
    button: { fontWeight: 600 },
  },
  shape: { borderRadius: 12 },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        body: {
          backgroundColor: colors.bg,
          // Faint brand glow at the top for depth without noise.
          backgroundImage: `radial-gradient(1200px circle at 15% -10%, ${alpha(
            colors.accentBlue,
            0.07
          )}, transparent 42%), radial-gradient(1000px circle at 100% 0%, ${alpha(
            colors.accentIndigo,
            0.05
          )}, transparent 40%)`,
          backgroundAttachment: "fixed",
          color: colors.textPrimary,
        },
        "*::-webkit-scrollbar": { width: 10, height: 10 },
        "*::-webkit-scrollbar-track": { background: "transparent" },
        "*::-webkit-scrollbar-thumb": {
          background: "#D3DBE6",
          borderRadius: 8,
          border: `2px solid ${colors.bg}`,
        },
        "*::-webkit-scrollbar-thumb:hover": { background: "#B9C3D3" },
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
          boxShadow: "0 1px 2px rgba(16,24,40,0.03), 0 6px 20px rgba(16,24,40,0.05)",
          transition: "box-shadow 0.2s ease, border-color 0.2s ease",
          "&:hover": { boxShadow: "0 2px 6px rgba(16,24,40,0.05), 0 12px 30px rgba(16,24,40,0.09)" },
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: { backgroundImage: "none" },
        elevation0: { boxShadow: "none" },
      },
    },
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: { textTransform: "none", fontWeight: 600, borderRadius: 10 },
        containedPrimary: {
          background: gradients.brand,
          boxShadow: "0 2px 10px rgba(10,166,186,0.28)",
          "&:hover": { background: gradients.brandHover, boxShadow: "0 4px 14px rgba(10,166,186,0.34)" },
        },
        outlined: { borderColor: colors.border },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: { fontWeight: 600, borderRadius: 8 },
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
          backgroundColor: "#F6F9FC",
        },
      },
    },
    MuiTableRow: {
      styleOverrides: {
        root: { "&:hover": { backgroundColor: alpha(colors.accentBlue, 0.05) } },
      },
    },
    MuiLinearProgress: {
      styleOverrides: {
        root: { borderRadius: 6, height: 8, backgroundColor: "#E8EDF4" },
      },
    },
    MuiAlert: {
      styleOverrides: {
        root: { borderRadius: 12, border: `1px solid ${colors.border}` },
      },
    },
    MuiOutlinedInput: {
      styleOverrides: {
        root: {
          backgroundColor: colors.surface,
          borderRadius: 10,
          "& .MuiOutlinedInput-notchedOutline": { borderColor: colors.border },
        },
      },
    },
    MuiTooltip: {
      styleOverrides: {
        tooltip: {
          backgroundColor: colors.textPrimary,
          color: "#FFFFFF",
          fontSize: 12,
          borderRadius: 8,
          padding: "6px 10px",
        },
        arrow: { color: colors.textPrimary },
      },
    },
  },
});

// Shared chart helpers for Recharts on the light surface.
export const chartTheme = {
  grid: colors.border,
  axis: colors.textSecondary,
  tooltipBg: colors.surface,
  tooltipBorder: colors.border,
};
