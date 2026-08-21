import { createTheme, alpha, Theme } from "@mui/material/styles";

// ---------------------------------------------------------------------------
// Azure CAT theme — one premium enterprise identity in TWO modes.
//
// Light: clean white surfaces on a soft blue-grey page. Dark: a sophisticated
// deep navy-charcoal foundation (never pure black) with the SAME cyan/teal brand
// accents, tuned a touch brighter so they read on dark. Components consume the
// mutable `colors` object (real hex, so MUI `alpha()` works); switching mode swaps
// the active hex + the MUI theme, and because every component styles via `sx`
// (which subscribes to the theme context), the whole app re-renders in the new mode.
// ---------------------------------------------------------------------------

export type ThemeMode = "light" | "dark";

export interface ColorSet {
  bg: string;
  surface: string;
  surfaceElevated: string;
  border: string;
  accentBlue: string;
  accentIndigo: string;
  success: string;
  warning: string;
  error: string;
  info: string;
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
}

const LIGHT: ColorSet = {
  bg: "#F4F7FB",
  surface: "#FFFFFF",
  surfaceElevated: "#F2F6FA",
  border: "#E6EBF2",
  accentBlue: "#0AA6BA",
  accentIndigo: "#5B7CFA",
  success: "#12B886",
  warning: "#F59E0B",
  error: "#EF4444",
  info: "#0EA5E9",
  textPrimary: "#0F1B2E",
  textSecondary: "#586A85",
  textMuted: "#9AA7BA",
};

const DARK: ColorSet = {
  bg: "#0E1621", // deep navy-charcoal — calm, not pure black
  surface: "#17212E", // cards / panels
  surfaceElevated: "#1F2A38", // inset / hover fills
  border: "#2A3A4A", // hairline borders
  accentBlue: "#16C8DA", // brand teal, a touch brighter for dark surfaces
  accentIndigo: "#8098FB",
  success: "#22D69E", // savings green, brighter on dark
  warning: "#FBB84C",
  error: "#F87171",
  info: "#38BDF8",
  textPrimary: "#E9EFF7", // soft white (not harsh #FFF)
  textSecondary: "#A7B6C9",
  textMuted: "#6E8095",
};

// The ACTIVE colour set consumed by every component. Mutable by design: `applyColorScheme` swaps its
// contents in place, and the MUI theme swap (below) drives the re-render that reads the new values.
export const colors: ColorSet = { ...LIGHT };

const STORAGE_KEY = "cat-theme-mode";

export function getStoredMode(): ThemeMode {
  try {
    return localStorage.getItem(STORAGE_KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function applyColorScheme(mode: ThemeMode): void {
  const set = mode === "dark" ? DARK : LIGHT;
  Object.assign(colors, set);
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    /* storage unavailable — theme still applies for the session */
  }
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("data-theme", mode);
    document.documentElement.style.colorScheme = mode; // native form controls / scrollbars
    // Deterministically reset the page surface + INHERITED text colour on every switch. MUI's
    // <CssBaseline> also sets these via an emotion global style, but that block does not reliably
    // re-apply on dark→light (emotion caches the first light block; the later dark block keeps winning
    // by source order), which left inherited body text light-on-light — the "washed out" symptom. An
    // inline style on <html>/<body> has higher precedence than the global block, so it always wins and
    // the transition is unambiguous in both directions.
    document.documentElement.style.backgroundColor = set.bg;
    if (document.body) {
      document.body.style.backgroundColor = set.bg;
      document.body.style.color = set.textPrimary;
    }
  }
}

// Brand gradient — the single source for the teal→cyan brand fill (constant across modes).
export const gradients = {
  brand: "linear-gradient(135deg, #16C8DA 0%, #0A97B6 100%)",
  brandHover: "linear-gradient(135deg, #12B7C8 0%, #08869F 100%)",
};

// Severity tokens (used only by non-cost surfaces / legacy table). Kept light-tuned; cost UI no longer
// surfaces severity, so these are not on the primary dark-mode path.
export const SEVERITY = {
  critical: { bg: "#FDECEC", text: "#B42318", border: "#F7D3CF", solid: "#E5484D" },
  high: { bg: "#FFF2E8", text: "#B54708", border: "#FBDBC1", solid: "#F7910A" },
  medium: { bg: "#FEF6E6", text: "#8A5A00", border: "#FBE6B2", solid: "#F5B301" },
  low: { bg: "#E7F6F9", text: "#0E7490", border: "#C2E9EF", solid: "#0BB8C4" },
} as const;

export type SeverityKey = keyof typeof SEVERITY;

function buildTheme(c: ColorSet, mode: ThemeMode): Theme {
  const dark = mode === "dark";
  const cardShadow = dark
    ? "0 1px 2px rgba(0,0,0,0.30), 0 8px 24px rgba(0,0,0,0.38)"
    : "0 1px 2px rgba(16,24,40,0.03), 0 6px 20px rgba(16,24,40,0.05)";
  const cardShadowHover = dark
    ? "0 2px 6px rgba(0,0,0,0.35), 0 14px 34px rgba(0,0,0,0.46)"
    : "0 2px 6px rgba(16,24,40,0.05), 0 12px 30px rgba(16,24,40,0.09)";

  return createTheme({
    palette: {
      mode,
      background: { default: c.bg, paper: c.surface },
      primary: { main: c.accentBlue, light: dark ? "#4FD8E8" : "#3FC6D6", dark: "#0A97B6", contrastText: "#062A30" },
      secondary: { main: c.accentIndigo, light: "#8AA0FB", dark: "#3F5BD0" },
      success: { main: c.success },
      warning: { main: c.warning },
      error: { main: c.error },
      info: { main: c.info },
      text: { primary: c.textPrimary, secondary: c.textSecondary },
      divider: c.border,
    },
    typography: {
      fontFamily: '"Inter", "Segoe UI", system-ui, sans-serif',
      h1: { fontWeight: 800, letterSpacing: "-0.6px", color: c.textPrimary },
      h2: { fontWeight: 800, letterSpacing: "-0.5px", color: c.textPrimary },
      h3: { fontWeight: 800, letterSpacing: "-0.5px", color: c.textPrimary },
      h4: { fontWeight: 700, letterSpacing: "-0.4px", color: c.textPrimary },
      h5: { fontWeight: 700, letterSpacing: "-0.3px", color: c.textPrimary },
      h6: { fontWeight: 700, color: c.textPrimary },
      subtitle1: { fontWeight: 600 },
      button: { fontWeight: 600 },
    },
    shape: { borderRadius: 12 },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          body: {
            backgroundColor: c.bg,
            backgroundImage: `radial-gradient(1200px circle at 15% -10%, ${alpha(c.accentBlue, dark ? 0.10 : 0.07)}, transparent 42%), radial-gradient(1000px circle at 100% 0%, ${alpha(c.accentIndigo, dark ? 0.08 : 0.05)}, transparent 40%)`,
            backgroundAttachment: "fixed",
            color: c.textPrimary,
          },
          "*::-webkit-scrollbar": { width: 10, height: 10 },
          "*::-webkit-scrollbar-track": { background: "transparent" },
          "*::-webkit-scrollbar-thumb": {
            background: dark ? "#33475C" : "#D3DBE6",
            borderRadius: 8,
            border: `2px solid ${c.bg}`,
          },
          "*::-webkit-scrollbar-thumb:hover": { background: dark ? "#415771" : "#B9C3D3" },
        },
      },
      MuiCard: {
        defaultProps: { elevation: 0 },
        styleOverrides: {
          root: {
            backgroundImage: "none",
            backgroundColor: c.surface,
            border: `1px solid ${c.border}`,
            borderRadius: 16,
            boxShadow: cardShadow,
            transition: "box-shadow 0.2s ease, border-color 0.2s ease",
            "&:hover": { boxShadow: cardShadowHover },
          },
        },
      },
      MuiPaper: {
        styleOverrides: {
          root: { backgroundImage: "none", backgroundColor: c.surface },
          elevation0: { boxShadow: "none" },
        },
      },
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: {
          root: { textTransform: "none", fontWeight: 600, borderRadius: 10 },
          containedPrimary: {
            background: gradients.brand,
            color: "#FFFFFF",
            boxShadow: "0 2px 10px rgba(10,166,186,0.28)",
            "&:hover": { background: gradients.brandHover, boxShadow: "0 4px 14px rgba(10,166,186,0.34)" },
          },
          outlined: { borderColor: c.border },
        },
      },
      MuiChip: {
        styleOverrides: { root: { fontWeight: 600, borderRadius: 8 } },
      },
      MuiTableCell: {
        styleOverrides: {
          root: { borderColor: c.border },
          head: {
            color: c.textSecondary,
            fontWeight: 600,
            fontSize: 12,
            letterSpacing: "0.04em",
            textTransform: "uppercase",
            backgroundColor: dark ? c.surfaceElevated : "#F6F9FC",
          },
        },
      },
      MuiTableRow: {
        styleOverrides: { root: { "&:hover": { backgroundColor: alpha(c.accentBlue, 0.06) } } },
      },
      MuiLinearProgress: {
        styleOverrides: {
          root: { borderRadius: 6, height: 8, backgroundColor: dark ? c.surfaceElevated : "#E8EDF4" },
        },
      },
      MuiAlert: {
        styleOverrides: { root: { borderRadius: 12, border: `1px solid ${c.border}` } },
      },
      MuiOutlinedInput: {
        styleOverrides: {
          root: {
            backgroundColor: c.surface,
            borderRadius: 10,
            "& .MuiOutlinedInput-notchedOutline": { borderColor: c.border },
          },
        },
      },
      MuiTooltip: {
        styleOverrides: {
          // Light: dark tooltip on a light page. Dark: an elevated dark tooltip with light text
          // (never the light `textPrimary` as a background, which would be unreadable).
          tooltip: {
            backgroundColor: dark ? "#26374A" : c.textPrimary,
            color: dark ? c.textPrimary : "#FFFFFF",
            fontSize: 12,
            borderRadius: 8,
            padding: "6px 10px",
            border: dark ? `1px solid ${c.border}` : "none",
          },
          arrow: { color: dark ? "#26374A" : c.textPrimary },
        },
      },
    },
  });
}

export const lightTheme = buildTheme(LIGHT, "light");
export const darkTheme = buildTheme(DARK, "dark");

export function themeFor(mode: ThemeMode): Theme {
  return mode === "dark" ? darkTheme : lightTheme;
}

// Apply the persisted mode NOW (module load) so the very first paint is already correct (no flash).
applyColorScheme(getStoredMode());

// Back-compat default export used before the provider mounts.
export const theme = lightTheme;

// Chart helpers (only used by the dead SummaryCards component; live charts read `colors` directly).
export const chartTheme = {
  grid: colors.border,
  axis: colors.textSecondary,
  tooltipBg: colors.surface,
  tooltipBorder: colors.border,
};
