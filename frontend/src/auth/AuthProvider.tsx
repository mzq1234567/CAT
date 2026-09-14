import React from "react";

/**
 * Device-code auth context. Replaces MSAL: the delegated ARM access token is obtained by the backend
 * device-code flow (see pages/Login.tsx + backend /api/auth/device/*) and handed to the browser, which
 * stores it here and sends it as `Authorization: Bearer` on every API call — exactly as before, so the
 * rest of the app is unchanged.
 *
 * The token lives in sessionStorage (tab-scoped, cleared on close), never localStorage. There is no
 * silent refresh (device code is interactive), so when the token expires the user signs in again.
 */

export interface Account {
  user_id: string;
  tenant_id: string;
  email: string;
}

export interface Session {
  access_token: string;
  expires_on: number; // epoch seconds
  account: Account;
}

interface AuthContextValue {
  account: Account | null;
  isAuthenticated: boolean;
  /** Returns the current ARM access token, or throws if not signed in / expired. */
  getToken: () => string;
  setSession: (session: Session) => void;
  logout: () => void;
}

const STORAGE_KEY = "cat-auth";
const CLOCK_SKEW_SECONDS = 60;

const AuthContext = React.createContext<AuthContextValue | null>(null);

function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Session;
    if (!parsed?.access_token || !parsed?.expires_on) return null;
    return parsed;
  } catch {
    return null;
  }
}

function isLive(session: Session | null): session is Session {
  return !!session && session.expires_on - CLOCK_SKEW_SECONDS > Date.now() / 1000;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSessionState] = React.useState<Session | null>(() => {
    const s = loadSession();
    return isLive(s) ? s : null;
  });

  const setSession = React.useCallback((next: Session) => {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {
      /* private mode / storage disabled — keep in memory only */
    }
    setSessionState(next);
  }, []);

  const logout = React.useCallback(() => {
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
    setSessionState(null);
  }, []);

  const getToken = React.useCallback(() => {
    if (!isLive(session)) {
      logout();
      throw new Error("Not authenticated");
    }
    return session.access_token;
  }, [session, logout]);

  const value = React.useMemo<AuthContextValue>(
    () => ({
      account: session?.account ?? null,
      isAuthenticated: isLive(session),
      getToken,
      setSession,
      logout,
    }),
    [session, getToken, setSession, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = React.useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
