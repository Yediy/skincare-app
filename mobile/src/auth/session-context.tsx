import { useQueryClient } from "@tanstack/react-query";
import React, { createContext, useContext, useEffect, useMemo, useState } from "react";

import { deleteAccount as deleteAccountApi, login, logout as logoutApi, signup } from "@/api/auth-api";
import { logger } from "@/utils/logger";

import { setSessionInvalidListener, tokenStorage } from "./auth-client-singleton";
import { restoreSession, type SessionState } from "./restore-session";

type SessionContextValue = {
  state: SessionState;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  deleteAccount: () => Promise<void>;
};

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<SessionState>({ status: "UNKNOWN" });
  // Created once in app/_layout.tsx via useState(() => ...), so this
  // identity is stable for the component tree's lifetime -- safe to
  // depend on without re-running restoreSession() on every render.
  const queryClient = useQueryClient();

  useEffect(() => {
    let cancelled = false;
    restoreSession(tokenStorage)
      .then((next) => {
        if (!cancelled) setState(next);
      })
      .catch((err) => {
        logger.error("session restoration crashed; defaulting to signed out", err);
        if (!cancelled) setState({ status: "SIGNED_OUT" });
      });

    setSessionInvalidListener(async () => {
      await queryClient.clear();
      setState({ status: "SIGNED_OUT" });
    });

    return () => {
      cancelled = true;
      setSessionInvalidListener(null);
    };
  }, [queryClient]);

  const value = useMemo<SessionContextValue>(
    () => ({
      state,
      async signIn(email, password) {
        const tokens = await login({ email, password });
        await tokenStorage.setTokenPair(tokens);
        setState({ status: "AUTHENTICATED" });
      },
      async signUp(email, password) {
        await signup({ email, password });
        const tokens = await login({ email, password });
        await tokenStorage.setTokenPair(tokens);
        setState({ status: "AUTHENTICATED" });
      },
      async signOut() {
        const refreshToken = await tokenStorage.getRefreshToken();
        if (refreshToken) {
          try {
            await logoutApi({ refresh_token: refreshToken });
          } catch (err) {
            // Best-effort server-side revocation -- the local session
            // ends regardless (below), so a network error here must
            // never block sign-out.
            logger.warn("server-side logout call failed; ending session locally anyway", err);
          }
        }
        await tokenStorage.clear();
        await queryClient.clear();
        setState({ status: "SIGNED_OUT" });
      },
      async deleteAccount() {
        await deleteAccountApi();
        await tokenStorage.clear();
        await queryClient.clear();
        setState({ status: "SIGNED_OUT" });
      },
    }),
    [state, queryClient],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) {
    throw new Error("useSession() must be used within a SessionProvider");
  }
  return ctx;
}
