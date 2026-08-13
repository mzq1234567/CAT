import React from "react";
import { Box, Typography } from "@mui/material";
import { keyframes } from "@mui/material/styles";
import { colors } from "../../theme";
import { Assessment } from "../../types";
import DiscoveryMetrics from "./DiscoveryMetrics";
import DnaStrand from "./DnaStrand";
import ProgressThread from "./ProgressThread";
import ResourceNodes from "./ResourceNodes";
import StageCaption from "./StageCaption";
import { STAGES, captionFor, stageIndexFor, sublinesFor } from "./stages";
import { useReducedMotion, useSequencedSwap, useSmoothProgress } from "./useAssessmentMotion";

/**
 * The running-assessment screen.
 *
 *   flow (+ resource nodes) → caption → rotating descriptor → progress → one statistic
 *
 * The flow is the subject and now fills much more of the column; the resource nodes around it are the
 * environment being assessed (real discovered types, or neutral placeholders until discovery returns).
 * The state underneath stays real: the caption follows the backend's actual stage, the descriptor rotates
 * within a stage without ever advancing progress, the bar can only enter the next band when the pipeline
 * genuinely advances, and the statistic is a measured count.
 */

const enter = keyframes`
  from { opacity: 0; transform: translateY(18px); }
  to   { opacity: 1; transform: none; }
`;

const brandDrift = keyframes`
  0%, 100% { opacity: 0.5; transform: translate3d(-2%, -1%, 0) scale(1); }
  50%      { opacity: 0.85; transform: translate3d(2%, 1%, 0) scale(1.08); }
`;

const subOut = keyframes`
  0%   { opacity: 1; transform: translateY(0); }
  100% { opacity: 0; transform: translateY(-6px); }
`;
const subIn = keyframes`
  0%   { opacity: 0; transform: translateY(6px); }
  100% { opacity: 1; transform: translateY(0); }
`;

/** A muted secondary line that rotates through a stage's descriptors — remounted per stage (via `key`)
 *  so it restarts cleanly on a real stage change. Never influences progress. */
function RotatingSubline({ lines, reduced }: { lines: string[]; reduced: boolean }) {
  const [i, setI] = React.useState(0);
  React.useEffect(() => {
    if (reduced || lines.length <= 1) return;
    const t = setInterval(() => setI((v) => (v + 1) % lines.length), 3200);
    return () => clearInterval(t);
  }, [reduced, lines.length]);

  const line = lines.length ? lines[i % lines.length] : "";
  const { shown, leaving } = useSequencedSwap(line, 240);

  return (
    <Box sx={{ height: 22, display: "flex", alignItems: "center", justifyContent: "center", width: "100%" }}>
      <Typography
        key={shown}
        variant="body2"
        sx={{
          color: colors.textMuted,
          letterSpacing: "0.005em",
          textAlign: "center",
          whiteSpace: "nowrap",
          animation: reduced
            ? "none"
            : leaving
            ? `${subOut} 240ms ease forwards`
            : `${subIn} .6s cubic-bezier(.16,1,.3,1)`,
        }}
      >
        {shown}
      </Typography>
    </Box>
  );
}

export default function AssessmentProgress({ assessment }: { assessment: Assessment }) {
  const reduced = useReducedMotion();
  const { status, progress } = assessment;

  const activeIndex = stageIndexFor(status);
  const ceiling = activeIndex >= 0 && activeIndex < STAGES.length - 1 ? STAGES[activeIndex + 1].at : 99;
  const pct = useSmoothProgress(progress || (status === "queued" ? 4 : 10), ceiling, reduced);

  const reveal = (delay: number) =>
    reduced ? "none" : `${enter} .85s cubic-bezier(.16,1,.3,1) ${delay}ms forwards`;

  return (
    <Box
      sx={{
        position: "relative",
        minHeight: "calc(100vh - 64px)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        px: 3,
        py: 2,
        overflow: "hidden",
      }}
    >
      {/* Brand-gradient ambient light — warmth on an otherwise cool composition. */}
      <Box
        aria-hidden
        sx={{
          position: "absolute",
          width: 1040,
          height: 1040,
          maxWidth: "160vw",
          borderRadius: "50%",
          pointerEvents: "none",
          background:
            "radial-gradient(circle, rgba(244,114,52,0.07) 0%, rgba(214,63,110,0.045) 30%, rgba(140,86,214,0.028) 46%, rgba(255,255,255,0) 62%)",
          filter: "blur(58px)",
          animation: reduced ? "none" : `${brandDrift} 34s ease-in-out infinite`,
          zIndex: 0,
        }}
      />

      {/* Composition */}
      <Box
        sx={{
          position: "relative",
          zIndex: 1,
          width: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
        }}
      >
        {/* Horizontal data helix + resource nodes — the subject, filling ~70% of the column width. */}
        <Box
          sx={{
            position: "relative",
            width: "100%",
            maxWidth: { xs: 520, sm: 720, md: 920, lg: 1040 },
            mx: "auto",
            opacity: reduced ? 1 : 0,
            animation: reveal(0),
          }}
        >
          <DnaStrand />
          <ResourceNodes assessment={assessment} />
        </Box>

        <Box sx={{ mt: { xs: 3, md: 3.5 }, width: "100%", opacity: reduced ? 1 : 0, animation: reveal(160) }}>
          <StageCaption text={captionFor(status)} />
        </Box>

        <Box sx={{ mt: 0.5, width: "100%", opacity: reduced ? 1 : 0, animation: reveal(240) }}>
          <RotatingSubline key={status} lines={sublinesFor(status)} reduced={reduced} />
        </Box>

        <Box sx={{ mt: { xs: 2.5, md: 3 }, opacity: reduced ? 1 : 0, animation: reveal(360) }}>
          <ProgressThread pct={pct} stage={activeIndex} />
        </Box>

        <Box sx={{ mt: { xs: 2.5, md: 3 }, width: "100%", opacity: reduced ? 1 : 0, animation: reveal(500) }}>
          <DiscoveryMetrics assessment={assessment} />
        </Box>
      </Box>
    </Box>
  );
}
