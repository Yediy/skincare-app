import { request } from "@/api/client";
import type { AuthTokens } from "@/types/domain";

import { createRefreshCoordinator } from "./refresh-coordinator";
import { createExpoSecureStoreAdapter } from "./secure-store-adapter";
import { createTokenStorage } from "./token-storage";

/**
 * The app's one production wiring of token storage + the refresh
 * coordinator, built from the real (native) SecureStore adapter. Any
 * code that needs an authenticated request goes through
 * `authorizedRequest` exported here rather than constructing its own
 * coordinator -- there is exactly one in-flight-refresh guard for the
 * whole app, which is the point.
 *
 * Deliberately not used by any unit test -- refresh-coordinator.ts's
 * own tests construct `createRefreshCoordinator` directly with a
 * fake in-memory adapter, so single-flight behavior is provable
 * without a real keychain.
 */
export async function postRefresh(refreshToken: string): Promise<AuthTokens> {
  return request<AuthTokens>("/refresh", { method: "POST", body: { refresh_token: refreshToken } });
}

export const tokenStorage = createTokenStorage(createExpoSecureStoreAdapter());

type SessionInvalidListener = () => void | Promise<void>;
let sessionInvalidListener: SessionInvalidListener | null = null;

/** SessionProvider registers itself here on mount so a refresh
 * failure can flip session state to SIGNED_OUT and clear the Query
 * cache -- this module has no React dependency of its own. */
export function setSessionInvalidListener(listener: SessionInvalidListener | null): void {
  sessionInvalidListener = listener;
}

const coordinator = createRefreshCoordinator({
  tokenStorage,
  refreshTokens: postRefresh,
  onSessionInvalid: async () => {
    if (sessionInvalidListener) {
      await sessionInvalidListener();
    }
  },
});

export const authorizedRequest = coordinator.authorizedRequest;
