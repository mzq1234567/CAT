import { Box } from "@mui/material";
import { keyframes } from "@mui/material/styles";
import { Assessment } from "../../types";
import DiscoveryMetrics from "./DiscoveryMetrics";
import FlowField from "./FlowField";
import ProgressThread from "./ProgressThread";
import StageCaption from "./StageCaption";
import { STAGES, captionFor, stageIndexFor } from "./stages";
import { useReducedMotion, useSmoothProgress } from "./useAssessmentMotion";

/**
 * The running-assessment screen — four elements on a lot of quiet.
 *
 *   flow → caption → progress → one statistic
 *
 * The stack is optically centred rather than merely vertically centred: the flow carries far more
 * visual weight than the three text-height rows below it, so the group is nudged up slightly to sit
 * where the eye expects the centre to be.
 *
 * There is deliberately NO logo watermark. A ghost TPT mark was tried behind the composition and
 * removed — at any opacity that made it visible it read as a stain on the page rather than as
 * branding. The only brand presence left is the ambient wash, which borrows the mark's warm
 * orange→rose gradient to offset an otherwise entirely cool-blue composition. It carries no shape,
 * so it warms the page without ever looking like a mislaid asset.
 *
 * The state underneath stays real: the caption follows the backend's actual stage, the bar can only
 * enter the next band when the pipeline genuinely advances, and the statistic is a measured count.
 */

const enter = keyframes`
  from { opacity: 0; transform: translateY(18px); }
  to   { opacity: 1; transform: none; }
`;

// Very slow brand-tinted ambient drift — perceptible only as the page feeling warm rather than flat.
const brandDrift = keyframes`
  0%, 100% { opacity: 0.5; transform: translate3d(-2%, -1%, 0) scale(1); }
  50%      { opacity: 0.85; transform: translate3d(2%, 1%, 0) scale(1.08); }
`;

export default function AssessmentProgress({ assessment }: { assessment: Assessment }) {
  const reduced = useReducedMotion();
  const { status, progress } = assessment;

  const activeIndex = stageIndexFor(status);
  const ceiling =
    activeIndex >= 0 && activeIndex < STAGES.length - 1 ? STAGES[activeIndex + 1].at : 99;
  const pct = useSmoothProgress(progress || (status === "queued" ? 4 : 10), ceiling, reduced);

  const reveal = (delay: number) =>
    reduced ? "none" : `${enter} .85s cubic-bezier(.16,1,.3,1) ${delay}ms forwards`;

  return (
    <Box
      sx={{
        position: "relative",
        // Fill the column Layout actually gives us (full height minus its `p: 4` = 64px), rather
        // than a vh fraction. A short container centres the stack within itself and then leaves the
        // remainder empty below, which is what made the composition sit high on the real page.
        minHeight: "calc(100vh - 64px)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        px: 3,
        overflow: "hidden",
      }}
    >
      {/* ── Subconscious branding ──────────────────────────────────────────────────────────── */}
      {/* Brand-gradient ambient light: the composition is otherwise entirely cool blue, so this
          warmth is what makes the screen feel like TPT rather than generic SaaS. */}
      <Box
        aria-hidden
        sx={{
          position: "absolute",
          width: 900,
          height: 900,
          maxWidth: "150vw",
          borderRadius: "50%",
          pointerEvents: "none",
          // Warmth only — kept weak so it tints the page without greying the type sitting on it.
          // The falloff must reach zero well inside the element's radius (was 76%, which left a
          // perceptible rounded edge that read as a panel boundary rather than as ambient light).
          background:
            "radial-gradient(circle, rgba(244,114,52,0.07) 0%, rgba(214,63,110,0.045) 30%, rgba(140,86,214,0.028) 46%, rgba(255,255,255,0) 62%)",
          filter: "blur(58px)",
          animation: reduced ? "none" : `${brandDrift} 34s ease-in-out infinite`,
          zIndex: 0,
        }}
      />
      {/* ── Composition ───────────────────────────────────────────────────────────────────── */}
      <Box
        sx={{
          position: "relative",
          zIndex: 1,
          width: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          // No optical lift here: an earlier -4 was tuned against a standalone preview and
          // overcorrected inside the real Layout, leaving the stack visibly high in its column.
        }}
      >
        <Box
          sx={{
            width: "100%",
            display: "flex",
            justifyContent: "center",
            opacity: reduced ? 1 : 0,
            animation: reveal(0),
          }}
        >
          <FlowField width={580} />
        </Box>

        <Box sx={{ mt: { xs: 3.5, md: 4 }, width: "100%", opacity: reduced ? 1 : 0, animation: reveal(160) }}>
          <StageCaption text={captionFor(status)} />
        </Box>

        <Box sx={{ mt: { xs: 2.5, md: 3 }, opacity: reduced ? 1 : 0, animation: reveal(300) }}>
          <ProgressThread pct={pct} stage={activeIndex} />
        </Box>

        <Box sx={{ mt: { xs: 3, md: 3.5 }, width: "100%", opacity: reduced ? 1 : 0, animation: reveal(440) }}>
          <DiscoveryMetrics assessment={assessment} />
        </Box>
      </Box>
    </Box>
  );
}
