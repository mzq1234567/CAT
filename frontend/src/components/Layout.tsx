import React from "react";
import {
  Avatar, Box, Button, Stack, ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import { useAuth } from "../auth/AuthProvider";
import { useLocation, useNavigate } from "react-router-dom";
import DashboardIcon from "@mui/icons-material/Dashboard";
import AssessmentIcon from "@mui/icons-material/Assessment";
import AddCircleOutlineIcon from "@mui/icons-material/AddCircleOutline";
import LogoutIcon from "@mui/icons-material/Logout";
import LightModeOutlinedIcon from "@mui/icons-material/LightModeOutlined";
import DarkModeOutlinedIcon from "@mui/icons-material/DarkModeOutlined";
import { colors, gradients } from "../theme";
import { useThemeMode } from "./themeMode";
import tptLogo from "../assets/tpt-logo-dark.png";

/** Compact Light/Dark segmented control for the sidebar footer. */
function ThemeToggle() {
  const { mode, setMode } = useThemeMode();
  return (
    <ToggleButtonGroup
      size="small"
      exclusive
      value={mode}
      onChange={(_, v) => v && setMode(v)}
      fullWidth
      sx={{
        "& .MuiToggleButton-root": {
          textTransform: "none",
          fontWeight: 600,
          fontSize: 12,
          gap: 0.75,
          py: 0.6,
          color: colors.textSecondary,
          borderColor: colors.border,
          "&.Mui-selected": {
            color: colors.accentBlue,
            bgcolor: alpha(colors.accentBlue, 0.14),
            "&:hover": { bgcolor: alpha(colors.accentBlue, 0.2) },
          },
        },
      }}
    >
      <ToggleButton value="light">
        <LightModeOutlinedIcon sx={{ fontSize: 16 }} /> Light
      </ToggleButton>
      <ToggleButton value="dark">
        <DarkModeOutlinedIcon sx={{ fontSize: 16 }} /> Dark
      </ToggleButton>
    </ToggleButtonGroup>
  );
}

const SIDEBAR_WIDTH = 240;

interface Props {
  children: React.ReactNode;
  title?: string;
  subtitle?: string;
  actions?: React.ReactNode;
}

interface NavItem {
  label: string;
  icon: React.ReactNode;
  path: string;
  match: (pathname: string) => boolean;
}

const NAV: NavItem[] = [
  {
    label: "Dashboard",
    icon: <DashboardIcon fontSize="small" />,
    path: "/subscriptions",
    match: (p) => p === "/" || p.startsWith("/subscriptions"),
  },
  {
    label: "Assessments",
    icon: <AssessmentIcon fontSize="small" />,
    path: "/assessments",
    match: (p) => p.startsWith("/assessments"),
  },
  {
    label: "New Assessment",
    icon: <AddCircleOutlineIcon fontSize="small" />,
    path: "/subscriptions",
    match: () => false,
  },
];

export default function Layout({ children, title, subtitle, actions }: Props) {
  const { account, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const { mode } = useThemeMode();
  // The only logo asset is the dark-ink wordmark (built for light surfaces); on the dark sidebar it
  // would be invisible, so render it as a clean light wordmark in dark mode.
  const logoFilter = mode === "dark" ? "brightness(0) invert(1)" : "none";

  const displayName = account?.email?.split("@")[0] ?? "Signed in";
  const initials =
    displayName
      .split(/[.\-_ ]/)
      .map((n) => n[0])
      .slice(0, 2)
      .join("")
      .toUpperCase() || "?";

  return (
    <Box sx={{ display: "flex", minHeight: "100vh", bgcolor: colors.bg }}>
      {/* Sidebar */}
      <Box
        component="nav"
        sx={{
          width: SIDEBAR_WIDTH,
          flexShrink: 0,
          bgcolor: colors.surface,
          borderRight: `1px solid ${colors.border}`,
          display: "flex",
          flexDirection: "column",
          position: "sticky",
          top: 0,
          height: "100vh",
        }}
      >
        {/* Brand header — vendor logo (dark-text version) sitting directly on the white sidebar,
            product name beneath it. One brand mark, at the top. */}
        <Box
          sx={{
            px: 2.5,
            py: 2.75,
            borderBottom: `1px solid ${colors.border}`,
          }}
        >
          <Box
            component="img"
            src={tptLogo}
            alt="Tech Plus Talent"
            sx={{ display: "block", width: "100%", maxWidth: 168, height: "auto", filter: logoFilter }}
          />
          <Typography
            fontSize={10}
            sx={{
              mt: 1.25,
              letterSpacing: "0.14em",
              textTransform: "uppercase",
              color: colors.textMuted,
            }}
          >
            Azure Cost Assessment
          </Typography>
        </Box>

        {/* Nav items */}
        <Stack spacing={0.5} sx={{ p: 1.5, flexGrow: 1 }}>
          {NAV.map((item) => {
            const active = item.match(location.pathname);
            return (
              <Box
                key={item.label}
                onClick={() => navigate(item.path)}
                sx={{
                  display: "flex",
                  alignItems: "center",
                  gap: 1.5,
                  px: 1.5,
                  py: 1.1,
                  borderRadius: 2,
                  cursor: "pointer",
                  position: "relative",
                  color: active ? colors.accentBlue : colors.textSecondary,
                  bgcolor: active ? alpha(colors.accentBlue, 0.15) : "transparent",
                  fontWeight: active ? 600 : 500,
                  transition: "all 0.15s ease",
                  "&:before": active
                    ? {
                        content: '""',
                        position: "absolute",
                        left: 0,
                        top: 8,
                        bottom: 8,
                        width: 3,
                        borderRadius: 4,
                        bgcolor: colors.accentBlue,
                      }
                    : undefined,
                  "&:hover": {
                    bgcolor: active
                      ? alpha(colors.accentBlue, 0.2)
                      : alpha(colors.textSecondary, 0.08),
                    color: active ? colors.accentBlue : colors.textPrimary,
                  },
                }}
              >
                {item.icon}
                <Typography variant="body2" fontWeight="inherit">
                  {item.label}
                </Typography>
              </Box>
            );
          })}
        </Stack>

        {/* Appearance — Light / Dark theme toggle */}
        <Box sx={{ px: 1.5, pb: account ? 0 : 1.5 }}>
          <ThemeToggle />
        </Box>

        {/* User card */}
        {account && (
          <Box sx={{ p: 1.5 }}>
            <Box
              sx={{
                p: 1.5,
                borderRadius: 2.5,
                border: `1px solid ${colors.border}`,
                bgcolor: colors.surfaceElevated,
              }}
            >
              <Box display="flex" alignItems="center" gap={1.25} mb={1.25}>
                <Avatar
                  sx={{
                    width: 34,
                    height: 34,
                    fontSize: 13,
                    background: gradients.brand,
                  }}
                >
                  {initials}
                </Avatar>
                <Box sx={{ minWidth: 0 }}>
                  <Typography
                    variant="body2"
                    fontWeight={600}
                    noWrap
                    color={colors.textPrimary}
                  >
                    {displayName}
                  </Typography>
                  <Tooltip title={account.email}>
                    <Typography variant="caption" color={colors.textMuted} noWrap component="div">
                      {account.email}
                    </Typography>
                  </Tooltip>
                </Box>
              </Box>
              <Button
                fullWidth
                size="small"
                variant="outlined"
                color="inherit"
                startIcon={<LogoutIcon fontSize="small" />}
                onClick={() => logout()}
                sx={{
                  color: colors.textSecondary,
                  borderColor: colors.border,
                  "&:hover": { borderColor: colors.textSecondary, bgcolor: alpha(colors.error, 0.1) },
                }}
              >
                Sign out
              </Button>
            </Box>
          </Box>
        )}
      </Box>

      {/* Main content */}
      <Box sx={{ flexGrow: 1, minWidth: 0, p: 4 }}>
        {(title || actions) && (
          <Box
            display="flex"
            justifyContent="space-between"
            alignItems="flex-start"
            flexWrap="wrap"
            gap={2}
            mb={4}
          >
            <Box>
              {title && (
                <Typography variant="h4" fontWeight={700} color={colors.textPrimary}>
                  {title}
                </Typography>
              )}
              {subtitle && (
                <Typography variant="body2" color="text.secondary" mt={0.5}>
                  {subtitle}
                </Typography>
              )}
            </Box>
            {actions && <Box display="flex" gap={1.5}>{actions}</Box>}
          </Box>
        )}
        {children}
      </Box>
    </Box>
  );
}
