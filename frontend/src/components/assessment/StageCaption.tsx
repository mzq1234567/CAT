import React from "react";
import { Box } from "@mui/material";
import { keyframes } from "@mui/material/styles";
import { colors } from "../../theme";
import { useReducedMotion, useSequencedSwap } from "./useAssessmentMotion";

/**
 * The single caption.
 *
 * Strictly one line exists at a time: the outgoing caption fades and blurs away, is unmounted, and
 * only then does the next fade and slide in. An earlier version overlapped the two, which looked
 * like a rendering fault. The slot has a fixed height so the handover can never shift the layout.
 */

const EXIT_MS = 300;

const out = keyframes`
  0%   { opacity: 1; transform: translateY(0); filter: blur(0px); }
  100% { opacity: 0; transform: translateY(-10px); filter: blur(6px); }
`;

const enter = keyframes`
  0%   { opacity: 0; transform: translateY(12px); filter: blur(7px); }
  55%  { opacity: 1; }
  100% { opacity: 1; transform: translateY(0); filter: blur(0px); }
`;

/** Memoised: depends only on `text`, but its parent re-renders on every poll and progress publish. */
function StageCaption({ text }: { text: string }) {
  const reduced = useReducedMotion();
  const { shown, leaving } = useSequencedSwap(text, EXIT_MS);

  return (
    <Box
      sx={{
        height: 34,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: "100%",
      }}
    >
      <Box
        key={shown}
        sx={{
          fontSize: { xs: "1.0625rem", md: "1.1875rem" },
          fontWeight: 500,
          letterSpacing: "-0.015em",
          color: colors.textPrimary,
          textAlign: "center",
          whiteSpace: "nowrap",
          animation: reduced
            ? "none"
            : leaving
            ? `${out} ${EXIT_MS}ms cubic-bezier(.4,0,1,1) forwards`
            : `${enter} .72s cubic-bezier(.16,1,.3,1)`,
        }}
      >
        {shown}
        <Box component="span" sx={{ opacity: 0.35 }}>
          …
        </Box>
      </Box>
    </Box>
  );
}

export default React.memo(StageCaption);
