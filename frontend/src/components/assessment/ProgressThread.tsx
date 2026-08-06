import { Box } from "@mui/material";
import { keyframes } from "@mui/material/styles";
import { useReducedMotion } from "./useAssessmentMotion";

/**
 * A progress indicator built as light rather than as a bar.
 *
 * A default track-and-fill reads as a component; this reads as a filament. The filled span glows,
 * a highlight travels along it continuously so it never looks frozen between backend updates, and a
 * soft head sits at the leading edge like the tip of a drawn line. Width changes ride a long
 * cubic-bezier so progress arrives rather than jumps, and each stage change sends a brighter pulse
 * down the thread.
 */

const travel = keyframes`
  0%   { transform: translateX(-140%); }
  100% { transform: translateX(320%); }
`;

// The gradient itself flows through the filament, so the bar reads as current even while its width
// is unchanged between backend polls.
const flow = keyframes`
  0%   { background-position: 0% 50%; }
  100% { background-position: 200% 50%; }
`;

const headGlow = keyframes`
  0%, 100% { opacity: 0.75; transform: translate(-50%, -50%) scale(1); }
  50%      { opacity: 1; transform: translate(-50%, -50%) scale(1.25); }
`;

const stagePulse = keyframes`
  0%   { opacity: 0; transform: translateX(-100%); }
  25%  { opacity: 1; }
  100% { opacity: 0; transform: translateX(140%); }
`;

const WIDTH = 208;

export default function ProgressThread({
  pct,
  stage,
  fluid = false,
}: {
  pct: number;
  stage: number;
  /** Stretch to the container instead of a fixed width. */
  fluid?: boolean;
}) {
  const reduced = useReducedMotion();
  const clamped = Math.max(0, Math.min(100, pct));

  return (
    <Box
      sx={{
        position: "relative",
        width: fluid ? "100%" : WIDTH,
        height: 12,
        display: "flex",
        alignItems: "center",
      }}
    >
      {/* Track — barely there */}
      <Box
        sx={{
          position: "absolute",
          inset: "auto 0",
          height: 1.5,
          borderRadius: 99,
          background:
            "linear-gradient(90deg, rgba(15,27,46,0.06), rgba(15,27,46,0.13) 50%, rgba(15,27,46,0.06))",
        }}
      />

      {/* Filled filament */}
      <Box
        sx={{
          position: "absolute",
          left: 0,
          height: 2,
          width: `${clamped}%`,
          borderRadius: 99,
          overflow: "hidden",
          background: "linear-gradient(90deg, #31D8FF, #0B63E5, #31D8FF, #0B63E5)",
          backgroundSize: "200% 100%",
          boxShadow: "0 0 10px rgba(49,216,255,0.65), 0 0 22px rgba(11,99,229,0.35)",
          transition: "width 1.4s cubic-bezier(.22,1,.36,1)",
          animation: reduced ? "none" : `${flow} 6s linear infinite`,
        }}
      >
        {/* Highlight travelling along the filament — motion even between backend updates */}
        {!reduced && (
          <Box
            sx={{
              position: "absolute",
              inset: 0,
              width: "45%",
              background:
                "linear-gradient(90deg, rgba(255,255,255,0) 0%, rgba(255,255,255,0.9) 50%, rgba(255,255,255,0) 100%)",
              animation: `${travel} 2.9s cubic-bezier(.45,0,.55,1) infinite`,
              willChange: "transform",
            }}
          />
        )}
        {/* Brighter pulse fired on each stage change */}
        {!reduced && (
          <Box
            key={`pulse-${stage}`}
            sx={{
              position: "absolute",
              inset: 0,
              width: "60%",
              background:
                "linear-gradient(90deg, rgba(255,255,255,0) 0%, rgba(255,255,255,1) 50%, rgba(255,255,255,0) 100%)",
              opacity: 0,
              animation: `${stagePulse} 1.5s cubic-bezier(.2,.8,.3,1)`,
            }}
          />
        )}
      </Box>

      {/* Leading head */}
      <Box
        sx={{
          position: "absolute",
          top: "50%",
          left: `${clamped}%`,
          width: 7,
          height: 7,
          borderRadius: "50%",
          background: "radial-gradient(circle, #FFFFFF 0%, #45DBFF 45%, rgba(69,219,255,0) 72%)",
          boxShadow: "0 0 12px rgba(69,219,255,0.9)",
          transform: "translate(-50%, -50%)",
          transition: "left 1.4s cubic-bezier(.22,1,.36,1)",
          animation: reduced ? "none" : `${headGlow} 2.4s ease-in-out infinite`,
          willChange: "transform, opacity",
        }}
      />
    </Box>
  );
}
