import React from "react";
import { Box, Button, Typography } from "@mui/material";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline";
import { colors } from "../theme";

// Catches render-time crashes so a bug shows a calm, branded fallback with a way out — never a white
// screen or a raw React error overlay in front of a client.
export default class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean }
> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error: unknown) {
    // Log for diagnostics; the client only ever sees the friendly fallback below.
    // eslint-disable-next-line no-console
    console.error("Render error:", error);
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    return (
      <Box
        display="flex"
        flexDirection="column"
        alignItems="center"
        justifyContent="center"
        gap={2}
        sx={{ minHeight: "100vh", px: 3, textAlign: "center", bgcolor: colors.bg }}
      >
        <ErrorOutlineIcon sx={{ fontSize: 56, color: colors.warning }} />
        <Typography variant="h5" fontWeight={800} color={colors.textPrimary}>
          Something went wrong
        </Typography>
        <Typography variant="body2" color="text.secondary" maxWidth={440}>
          The page ran into an unexpected problem. Reloading usually fixes it — your data is safe.
        </Typography>
        <Button variant="contained" onClick={() => window.location.reload()} sx={{ mt: 1 }}>
          Reload
        </Button>
      </Box>
    );
  }
}
