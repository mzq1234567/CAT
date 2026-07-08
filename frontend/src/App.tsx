import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { MsalProvider, useIsAuthenticated, useMsal } from "@azure/msal-react";
import { PublicClientApplication } from "@azure/msal-browser";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider, CssBaseline } from "@mui/material";

import { theme } from "./theme";
import { msalConfig } from "./auth/msalConfig";
import Login from "./pages/Login";
import SelectSubscriptions from "./pages/SelectSubscriptions";
import Results from "./pages/Results";

const msalInstance = new PublicClientApplication(msalConfig);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 30_000,
    },
  },
});

function AuthGate({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useIsAuthenticated();
  const { inProgress } = useMsal();

  if (inProgress !== "none") return null; // MSAL is initialising
  if (!isAuthenticated) return <Login />;
  return <>{children}</>;
}

export default function App() {
  return (
    <MsalProvider instance={msalInstance}>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider theme={theme}>
          <CssBaseline />
          <BrowserRouter>
            <AuthGate>
              <Routes>
                <Route path="/" element={<Navigate to="/subscriptions" replace />} />
                <Route path="/subscriptions" element={<SelectSubscriptions />} />
                <Route path="/assessments/:id" element={<Results />} />
              </Routes>
            </AuthGate>
          </BrowserRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </MsalProvider>
  );
}
