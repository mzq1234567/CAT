import React from "react";
import {
  Avatar, Box, Button, Stack, Tooltip, Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import { useMsal } from "@azure/msal-react";
import { useLocation, useNavigate } from "react-router-dom";
import CloudQueueIcon from "@mui/icons-material/CloudQueue";
import DashboardIcon from "@mui/icons-material/Dashboard";
import AssessmentIcon from "@mui/icons-material/Assessment";
import AddCircleOutlineIcon from "@mui/icons-material/AddCircleOutline";
import LogoutIcon from "@mui/icons-material/Logout";
import { colors } from "../theme";

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
  const { instance, accounts } = useMsal();
  const navigate = useNavigate();
  const location = useLocation();
  const account = accounts[0];

  const initials =
    account?.name
      ?.split(" ")
      .map((n) => n[0])
      .slice(0, 2)
      .join("")
      .toUpperCase() ?? "?";

  return (
    <Box sx={{ display: "flex", minHeight: "100vh", bgcolor: colors.bg }}>
      {/* Sidebar */}
      <Box
        component="nav"
        sx={{
          width: SIDEBAR_WIDTH,
          flexShrink: 0,
          bgcolor: colors.bg,
          borderRight: `1px solid ${colors.border}`,
          display: "flex",
          flexDirection: "column",
          position: "sticky",
          top: 0,
          height: "100vh",
        }}
      >
        {/* Logo */}
        <Box
          sx={{
            px: 2.5,
            py: 3,
            display: "flex",
            alignItems: "center",
            gap: 1.5,
            borderBottom: `1px solid ${colors.border}`,
          }}
        >
          <Box
            sx={{
              width: 38,
              height: 38,
              borderRadius: 2,
              background: "linear-gradient(135deg, #3B82F6 0%, #6366F1 100%)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <CloudQueueIcon sx={{ color: "#fff", fontSize: 22 }} />
          </Box>
          <Box>
            <Typography fontWeight={700} fontSize={16} color={colors.textPrimary} lineHeight={1.1}>
              Azure CAT
            </Typography>
            <Typography fontSize={11} color={colors.textMuted} letterSpacing="0.04em">
              COST ASSESSMENT
            </Typography>
          </Box>
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

        {/* User card */}
        {account && (
          <Box sx={{ p: 1.5 }}>
            <Box
              sx={{
                p: 1.5,
                borderRadius: 2,
                border: `1px solid ${colors.border}`,
                bgcolor: colors.surface,
              }}
            >
              <Box display="flex" alignItems="center" gap={1.25} mb={1.25}>
                <Avatar
                  sx={{
                    width: 34,
                    height: 34,
                    fontSize: 13,
                    background: "linear-gradient(135deg, #3B82F6 0%, #6366F1 100%)",
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
                    {account.name ?? "Signed in"}
                  </Typography>
                  <Tooltip title={account.username}>
                    <Typography variant="caption" color={colors.textMuted} noWrap component="div">
                      {account.username}
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
                onClick={() => instance.logoutRedirect()}
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
