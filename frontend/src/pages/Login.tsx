import { Box, Button, Paper, Stack, Typography } from "@mui/material";
import { useMsal } from "@azure/msal-react";
import MicrosoftIcon from "@mui/icons-material/Window";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import { loginRequest } from "../auth/msalConfig";
import { colors } from "../theme";
import tptLogo from "../assets/tpt-logo-dark.png";

export default function Login() {
  const { instance } = useMsal();

  const handleLogin = () => {
    instance.loginRedirect(loginRequest).catch(console.error);
  };

  const points = [
    "Read-only — uses your existing Azure Reader access",
    "Estimate monthly & annual savings in minutes",
    "Export investor-ready PDF reports",
  ];

  return (
    <Box
      display="flex"
      alignItems="center"
      justifyContent="center"
      minHeight="100vh"
      sx={{ bgcolor: colors.bg, p: 2 }}
    >
      <Paper
        sx={{
          p: 5,
          borderRadius: 4,
          textAlign: "center",
          maxWidth: 440,
          width: "100%",
          bgcolor: colors.surface,
          border: `1px solid ${colors.border}`,
        }}
      >
        {/* Vendor brand — the client's first impression of whose tool this is (dark-text logo on
            the white card). */}
        <Box
          component="img"
          src={tptLogo}
          alt="Tech Plus Talent"
          sx={{ display: "block", width: "100%", maxWidth: 250, mx: "auto", mb: 3.5, height: "auto" }}
        />

        <Typography variant="h5" fontWeight={700} color={colors.textPrimary} mb={1}>
          Azure Cost Assessment
        </Typography>
        <Typography variant="body2" color="text.secondary" mb={3}>
          Discover savings opportunities across your Azure subscriptions.
          Sign in with your Microsoft account to get started.
        </Typography>

        <Stack spacing={1.25} sx={{ textAlign: "left", mb: 4 }}>
          {points.map((p) => (
            <Box key={p} display="flex" alignItems="center" gap={1.25}>
              <CheckCircleIcon sx={{ fontSize: 18, color: colors.success }} />
              <Typography variant="body2" color="text.secondary">
                {p}
              </Typography>
            </Box>
          ))}
        </Stack>

        <Button
          variant="contained"
          size="large"
          fullWidth
          startIcon={<MicrosoftIcon />}
          onClick={handleLogin}
          sx={{ py: 1.5, fontSize: 15 }}
        >
          Sign in with Microsoft
        </Button>

        <Typography variant="caption" color={colors.textMuted} display="block" mt={3}>
          Uses your existing Azure Reader access. No additional permissions required.
        </Typography>
      </Paper>
    </Box>
  );
}
