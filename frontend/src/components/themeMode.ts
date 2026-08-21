import React from "react";
import type { ThemeMode } from "../theme";

/**
 * Theme-mode context. The mode STATE lives in <App> (not a nested provider) on purpose: a nested
 * provider whose `children` are passed in cannot re-render those children on a state change (React
 * bails on referentially-stable children), which left every `colors.*`-in-`sx` value baked in the old
 * mode after a toggle. Owning the state at App means a toggle re-renders the whole tree (a re-render,
 * NOT a remount — component state, scroll and the running animation are preserved) so all `sx` colours
 * are re-evaluated in the new mode.
 */
export interface ThemeModeCtx {
  mode: ThemeMode;
  setMode: (m: ThemeMode) => void;
  toggle: () => void;
}

export const ThemeModeContext = React.createContext<ThemeModeCtx>({
  mode: "light",
  setMode: () => {},
  toggle: () => {},
});

export const useThemeMode = () => React.useContext(ThemeModeContext);
