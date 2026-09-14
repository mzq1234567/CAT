import React from "react";
import { Alert, Box, Button, CircularProgress, Paper, Stack, Typography } from "@mui/material";
import { keyframes } from "@mui/material/styles";
import MicrosoftIcon from "@mui/icons-material/Window";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import CheckIcon from "@mui/icons-material/Check";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import InsightsOutlinedIcon from "@mui/icons-material/InsightsOutlined";
import SavingsOutlinedIcon from "@mui/icons-material/SavingsOutlined";
import ShieldOutlinedIcon from "@mui/icons-material/ShieldOutlined";
import { useAuth } from "../auth/AuthProvider";
import { DeviceLoginError, DeviceStart, pollDeviceLogin, startDeviceLogin } from "../auth/deviceLogin";
import { useReducedMotion } from "../components/assessment/useAssessmentMotion";
import { useThemeMode } from "../components/themeMode";
import { colors } from "../theme";
import tptLogo from "../assets/tpt-logo-dark.png";

/**
 * Sign-in — a calm, premium first impression. The card composition is unchanged; the page now carries
 * a very restrained ambient data-flow behind it (faint Azure-tinted curves + a few resource nodes) so
 * it reads as an intelligent cloud product rather than a static form. The animation is understated and
 * never competes with the Microsoft sign-in button; it renders as a still under prefers-reduced-motion.
 */

const drift = keyframes`
  0%, 100% { transform: translate3d(-1.2%, -0.8%, 0) scale(1); }
  50%      { transform: translate3d(1.2%, 0.8%, 0) scale(1.03); }
`;
const nodePulse = keyframes`
  0%, 100% { opacity: 0.3; }
  50%      { opacity: 0.8; }
`;
const shimmer = keyframes`
  0%   { stroke-dashoffset: 900; }
  100% { stroke-dashoffset: 0; }
`;

// Faint curved data paths sweeping the viewport, and a few resource nodes sitting on them. Off-canvas
// endpoints so nothing pops at the edges. Kept low-contrast so the sign-in card always dominates.
const PATHS = [
  "M -60 180 C 260 120, 560 300, 900 210 C 1180 140, 1400 260, 1700 190",
  "M -60 420 C 300 360, 560 470, 900 430 C 1240 395, 1420 470, 1700 430",
  "M -60 640 C 280 700, 600 560, 900 640 C 1220 720, 1440 600, 1700 660",
];
const NODES = [
  { x: 260, y: 150, d: 0 }, { x: 900, y: 232, d: 1.6 }, { x: 1360, y: 205, d: 3.1 },
  { x: 560, y: 452, d: 0.8 }, { x: 1180, y: 418, d: 2.3 },
  { x: 600, y: 612, d: 1.2 }, { x: 1180, y: 648, d: 2.9 },
];

function SignInBackdrop({ reduced }: { reduced: boolean }) {
  return (
    <Box
      aria-hidden
      sx={{
        position: "absolute",
        inset: 0,
        overflow: "hidden",
        pointerEvents: "none",
        zIndex: 0,
        // A soft cool wash so the curves sit on gentle depth rather than flat white.
        background:
          "radial-gradient(1100px circle at 18% 12%, rgba(11,99,229,0.05), transparent 46%), radial-gradient(1000px circle at 86% 88%, rgba(0,224,198,0.045), transparent 44%)",
      }}
    >
      <Box
        component="svg"
        viewBox="0 0 1600 800"
        preserveAspectRatio="xMidYMid slice"
        sx={{
          position: "absolute",
          inset: "-4% -4% -4% -4%",
          width: "108%",
          height: "108%",
          animation: reduced ? "none" : `${drift} 40s ease-in-out infinite`,
        }}
      >
        <defs>
          <linearGradient id="lb-guide" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#0B63E5" stopOpacity="0" />
            <stop offset="35%" stopColor="#31D8FF" stopOpacity="0.5" />
            <stop offset="65%" stopColor="#31D8FF" stopOpacity="0.5" />
            <stop offset="100%" stopColor="#0B63E5" stopOpacity="0" />
          </linearGradient>
        </defs>

        {/* Base curves — very faint, continuous */}
        {PATHS.map((d, i) => (
          <path key={`b-${i}`} d={d} fill="none" stroke="#0B63E5" strokeOpacity="0.08" strokeWidth="1.4" />
        ))}
        {/* A slow luminance travelling each curve (still under reduced-motion) */}
        {!reduced &&
          PATHS.map((d, i) => (
            <path
              key={`s-${i}`}
              d={d}
              fill="none"
              stroke="url(#lb-guide)"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeDasharray="120 900"
              style={{ animation: `${shimmer} ${26 + i * 5}s linear ${-i * 6}s infinite`, opacity: 0.5 }}
            />
          ))}
        {/* Resource nodes sitting on the paths — soft, slow pulse */}
        {NODES.map((n, i) => (
          <circle
            key={`n-${i}`}
            cx={n.x}
            cy={n.y}
            r={2.8}
            fill="#31D8FF"
            style={
              reduced
                ? { opacity: 0.4 }
                : { animation: `${nodePulse} ${7 + (i % 4)}s ease-in-out ${-n.d}s infinite` }
            }
          />
        ))}
      </Box>
    </Box>
  );
}

const POINTS = [
  { icon: <InsightsOutlinedIcon sx={{ fontSize: 18 }} />, text: "See where your Azure spend can be optimized" },
  { icon: <SavingsOutlinedIcon sx={{ fontSize: 18 }} />, text: "Quantify actionable savings, backed by your own Azure data" },
  { icon: <ShieldOutlinedIcon sx={{ fontSize: 18 }} />, text: "Read-only: nothing in your environment is changed" },
];

type Phase = "idle" | "starting" | "pending" | "error";

export default function Login() {
  const reduced = useReducedMotion();
  const { mode } = useThemeMode();
  const { setSession } = useAuth();

  const [phase, setPhase] = React.useState<Phase>("idle");
  const [device, setDevice] = React.useState<DeviceStart | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [copied, setCopied] = React.useState(false);

  // Kept in refs so the polling loop never reads stale values and can be cancelled on unmount/restart.
  const timerRef = React.useRef<number | undefined>(undefined);
  const deadlineRef = React.useRef<number>(0);
  const activeRef = React.useRef(false);

  const stopPolling = React.useCallback(() => {
    activeRef.current = false;
    if (timerRef.current !== undefined) {
      window.clearTimeout(timerRef.current);
      timerRef.current = undefined;
    }
  }, []);

  React.useEffect(() => () => stopPolling(), [stopPolling]);

  const poll = React.useCallback(
    async (sessionId: string, interval: number) => {
      if (!activeRef.current) return;
      if (Date.now() > deadlineRef.current) {
        stopPolling();
        setError("Your Microsoft sign-in code expired. Please start a new sign-in attempt.");
        setPhase("error");
        return;
      }
      try {
        const result = await pollDeviceLogin(sessionId);
        if (!activeRef.current) return;
        if (result.status === "complete" && result.access_token && result.account && result.expires_on) {
          stopPolling();
          // Hands the delegated ARM token to the app; AuthGate re-renders and unmounts this screen.
          setSession({
            access_token: result.access_token,
            expires_on: result.expires_on,
            account: result.account,
          });
          return;
        }
        timerRef.current = window.setTimeout(() => poll(sessionId, interval), interval * 1000);
      } catch (err) {
        if (!activeRef.current) return;
        stopPolling();
        setError(err instanceof DeviceLoginError ? err.message : "Microsoft sign-in could not be completed.");
        setPhase("error");
      }
    },
    [setSession, stopPolling]
  );

  const handleLogin = React.useCallback(async () => {
    stopPolling();
    setError(null);
    setCopied(false);
    setPhase("starting");
    try {
      const started = await startDeviceLogin();
      setDevice(started);
      setPhase("pending");
      deadlineRef.current = Date.now() + started.expires_in * 1000;
      activeRef.current = true;
      timerRef.current = window.setTimeout(
        () => poll(started.session_id, started.interval || 5),
        (started.interval || 5) * 1000
      );
    } catch (err) {
      setError(err instanceof DeviceLoginError ? err.message : "Could not start Microsoft sign-in.");
      setPhase("error");
    }
  }, [poll, stopPolling]);

  const copyCode = React.useCallback(async () => {
    if (!device) return;
    try {
      await navigator.clipboard.writeText(device.user_code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — the code is shown for manual entry */
    }
  }, [device]);

  return (
    <Box
      sx={{
        position: "relative",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "100vh",
        bgcolor: colors.bg,
        p: 2,
        overflow: "hidden",
      }}
    >
      <SignInBackdrop reduced={reduced} />

      <Paper
        sx={{
          position: "relative",
          zIndex: 1,
          p: { xs: 4, sm: 5 },
          borderRadius: 4,
          textAlign: "center",
          maxWidth: 452,
          width: "100%",
          bgcolor: colors.surface,
          border: `1px solid ${colors.border}`,
          boxShadow: "0 1px 2px rgba(16,24,40,0.04), 0 24px 60px rgba(16,24,40,0.10)",
        }}
      >
        {/* Vendor brand — the client's first impression of whose tool this is. */}
        <Box
          component="img"
          src={tptLogo}
          alt="Tech Plus Talent"
          sx={{
            display: "block", width: "100%", maxWidth: 240, mx: "auto", mb: 3.5, height: "auto",
            filter: mode === "dark" ? "brightness(0) invert(1)" : "none",
          }}
        />

        <Typography variant="h5" fontWeight={800} color={colors.textPrimary} mb={1} sx={{ letterSpacing: "-0.02em" }}>
          Azure Cost Assessment
        </Typography>
        <Typography variant="body1" color={colors.textPrimary} sx={{ fontWeight: 600, mb: 1 }}>
          Turn your Azure environment into a clear, evidence-based savings plan.
        </Typography>
        <Typography variant="body2" color="text.secondary" mb={3.5} sx={{ lineHeight: 1.6 }}>
          Connect with your Microsoft account to review your Azure environment using the access you
          already have.
        </Typography>

        <Stack spacing={1.5} sx={{ textAlign: "left", mb: 4 }}>
          {POINTS.map((p) => (
            <Box key={p.text} display="flex" alignItems="center" gap={1.5}>
              <Box
                sx={{
                  width: 30,
                  height: 30,
                  borderRadius: 1.5,
                  flexShrink: 0,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: colors.accentBlue,
                  bgcolor: "rgba(10,166,186,0.10)",
                }}
              >
                {p.icon}
              </Box>
              <Typography variant="body2" color={colors.textSecondary} sx={{ fontWeight: 500 }}>
                {p.text}
              </Typography>
            </Box>
          ))}
        </Stack>

        {phase === "pending" && device ? (
          <Stack spacing={2}>
            <Typography variant="body2" color={colors.textSecondary} sx={{ lineHeight: 1.6 }}>
              To sign in, open the Microsoft device-login page and enter this code:
            </Typography>

            <Box
              sx={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 1,
                px: 2,
                py: 1.5,
                borderRadius: 2,
                border: `1px solid ${colors.border}`,
                bgcolor: colors.surfaceElevated ?? colors.surface,
              }}
            >
              <Typography
                sx={{ fontFamily: "monospace", fontSize: 26, fontWeight: 700, letterSpacing: "0.12em", color: colors.textPrimary }}
              >
                {device.user_code}
              </Typography>
              <Button
                size="small"
                variant="text"
                startIcon={copied ? <CheckIcon fontSize="small" /> : <ContentCopyIcon fontSize="small" />}
                onClick={copyCode}
              >
                {copied ? "Copied" : "Copy"}
              </Button>
            </Box>

            <Button
              variant="contained"
              size="large"
              fullWidth
              startIcon={<OpenInNewIcon />}
              onClick={() => window.open(device.verification_uri, "_blank", "noopener,noreferrer")}
              sx={{ py: 1.5, fontSize: 15 }}
            >
              Open Microsoft sign-in
            </Button>

            <Box display="flex" alignItems="center" justifyContent="center" gap={1.25} mt={0.5}>
              <CircularProgress size={16} thickness={5} />
              <Typography variant="body2" color={colors.textMuted}>
                Waiting for Microsoft authentication…
              </Typography>
            </Box>

            <Button size="small" variant="text" color="inherit" onClick={() => { stopPolling(); setPhase("idle"); }}
              sx={{ color: colors.textMuted }}>
              Cancel
            </Button>
          </Stack>
        ) : phase === "error" ? (
          <Stack spacing={2}>
            <Alert severity="error" sx={{ textAlign: "left" }}>{error}</Alert>
            <Button variant="contained" size="large" fullWidth startIcon={<MicrosoftIcon />}
              onClick={handleLogin} sx={{ py: 1.5, fontSize: 15 }}>
              Try again
            </Button>
          </Stack>
        ) : (
          <Button
            variant="contained"
            size="large"
            fullWidth
            disabled={phase === "starting"}
            startIcon={phase === "starting" ? <CircularProgress size={18} color="inherit" /> : <MicrosoftIcon />}
            onClick={handleLogin}
            sx={{ py: 1.5, fontSize: 15 }}
          >
            {phase === "starting" ? "Starting sign-in…" : "Sign in with Microsoft"}
          </Button>
        )}

        <Typography variant="caption" color={colors.textMuted} display="block" mt={3} sx={{ lineHeight: 1.6 }}>
          Secure, read-only analysis using your existing Azure access, no changes are made to your
          resources.
        </Typography>
      </Paper>
    </Box>
  );
}
