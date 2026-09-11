import React from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider, CssBaseline } from "@mui/material";

import { AuthProvider, useAuth } from "./auth/AuthProvider";
import { applyColorScheme, getStoredMode, themeFor, ThemeMode } from "./theme";
import { ThemeModeContext } from "./components/themeMode";
import ErrorBoundary from "./components/ErrorBoundary";
import Login from "./pages/Login";
import SelectSubscriptions from "./pages/SelectSubscriptions";
import Assessments from "./pages/Assessments";
import Results from "./pages/Results";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 30_000,
    },
  },
});

function AuthGate({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAuth();
  if (!isAuthenticated) return <Login />;
  return <>{children}</>;
}

export default function App() {
  // Theme mode is owned HERE (the top of the tree) so a toggle re-renders the whole app — every
  // `colors.*`-in-`sx` value is re-evaluated in the new mode. It's a re-render, not a remount, so
  // component state, scroll position and the running assessment animation are all preserved.
  const [mode, setMode] = React.useState<ThemeMode>(() => getStoredMode());

  // Apply the palette synchronously during render (before children render) so `colors.*` and the MUI
  // theme are always the same mode within one render — no flash of the previous palette.
  const muiTheme = React.useMemo(() => {
    applyColorScheme(mode);
    return themeFor(mode);
  }, [mode]);

  const themeCtx = React.useMemo(
    () => ({ mode, setMode, toggle: () => setMode((m) => (m === "dark" ? "light" : "dark")) }),
    [mode]
  );

  return (
    <AuthProvider>
      <QueryClientProvider client={queryClient}>
        <ThemeModeContext.Provider value={themeCtx}>
          <ThemeProvider theme={muiTheme}>
            {/* key={mode} remounts CssBaseline so its emotion global `body` style is freshly inserted
                for the new mode (the source-level half of the dark→light body fix). */}
            <CssBaseline key={mode} />
            <BrowserRouter>
              <ErrorBoundary>
                <AuthGate>
                  <Routes>
                    <Route path="/" element={<Navigate to="/subscriptions" replace />} />
                    <Route path="/subscriptions" element={<SelectSubscriptions />} />
                    <Route path="/assessments" element={<Assessments />} />
                    <Route path="/assessments/:id" element={<Results />} />
                    {/* Any unknown route lands on a real page instead of a blank screen. */}
                    <Route path="*" element={<Navigate to="/subscriptions" replace />} />
                  </Routes>
                </AuthGate>
              </ErrorBoundary>
            </BrowserRouter>
          </ThemeProvider>
        </ThemeModeContext.Provider>
      </QueryClientProvider>
    </AuthProvider>
  );
}
