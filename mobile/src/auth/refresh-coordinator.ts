import { request, type RequestOptions } from "@/api/client";
import { ApiError, genericMessageFor } from "@/api/errors";
import type { AuthTokens } from "@/types/domain";
import { logger } from "@/utils/logger";

import type { TokenStorage } from "./token-storage";

export type RefreshCoordinatorDeps = {
  tokenStorage: TokenStorage;
  /** Calls POST /refresh. Kept as an injected function (rather than
   * this module importing auth-api.ts directly) purely to avoid a
   * circular import -- auth-api.ts's other functions go through
   * authorizedRequest, which this module defines. */
  refreshTokens: (refreshToken: string) => Promise<AuthTokens>;
  /** Invoked exactly when a refresh attempt itself fails -- never
   * when a post-refresh retry fails for some unrelated reason. The
   * session provider wires this to: clear SecureStore (already done
   * by this module before calling it), clear the authenticated Query
   * cache, and transition to SIGNED_OUT. */
  onSessionInvalid: () => void | Promise<void>;
};

/**
 * Bearer-token attachment + the backend's refresh-rotation contract,
 * in one place: at most one in-flight refresh call at a time, no
 * matter how many requests hit 401 simultaneously (concurrent 401s
 * share the same in-flight refresh promise rather than each firing
 * their own /refresh call, which would collide with the backend's
 * single-use rotation/replay defenses -- see backend/app/main.py's
 * /refresh docstring). Exactly one retry of the original request per
 * call; a 401 on the retry itself is never refreshed again.
 */
export function createRefreshCoordinator(deps: RefreshCoordinatorDeps) {
  let inFlightRefresh: Promise<string> | null = null;

  async function refreshOnce(): Promise<string> {
    if (inFlightRefresh) {
      return inFlightRefresh;
    }
    inFlightRefresh = (async () => {
      const refreshToken = await deps.tokenStorage.getRefreshToken();
      if (!refreshToken) {
        await deps.tokenStorage.clear();
        await deps.onSessionInvalid();
        throw new ApiError({
          status: 401,
          code: "UNAUTHORIZED",
          message: "Your session has expired. Please sign in again.",
          retryable: false,
        });
      }

      let tokens: AuthTokens;
      try {
        tokens = await deps.refreshTokens(refreshToken);
      } catch (err) {
        // A network outage, timeout, or backend 5xx while calling
        // /refresh proves NOTHING about the refresh token itself --
        // treating it as an invalid session would destroy a perfectly
        // good local session just because connectivity hiccuped
        // (section 22: "no network" must never be confused with
        // "invalid account"). Only a definite backend rejection of
        // the refresh token (401/403) ends the session here.
        const isDefiniteAuthFailure = err instanceof ApiError && (err.status === 401 || err.status === 403);
        if (isDefiniteAuthFailure) {
          logger.warn("refresh token rejected by backend; ending session", { status: (err as ApiError).status });
          await deps.tokenStorage.clear();
          await deps.onSessionInvalid();
        } else {
          logger.warn("refresh attempt failed transiently; session preserved", {
            code: err instanceof ApiError ? err.code : "UNKNOWN",
          });
        }
        throw err;
      }

      // The server has ALREADY consumed the old refresh token and
      // minted the new pair by this point -- there is no going back to
      // the old one. A failure to durably persist the new pair must
      // therefore never be treated as "retry with the old token" (it's
      // already dead server-side) -- fail closed: best-effort clear
      // whatever local state might be inconsistent, end the session,
      // and require normal re-authentication. Deliberately a SEPARATE
      // try/catch from the one above -- this failure must never be
      // reclassified by that block's transient-vs-definite-auth-failure
      // logic (which doesn't apply here at all; this isn't a rejection
      // by the backend, it's a local storage failure after success).
      try {
        await deps.tokenStorage.setTokenPair(tokens);
      } catch (storageErr) {
        logger.error(
          "failed to persist the rotated token pair after a successful refresh; the old refresh token is " +
            "already consumed server-side and will not be retried -- ending the session",
          storageErr,
        );
        await deps.tokenStorage.clear().catch(() => {
          // Best-effort: the thing that just failed was a SecureStore
          // write, so a subsequent SecureStore delete may fail too --
          // onSessionInvalid()/SIGNED_OUT below is what actually makes
          // the app stop trusting any stale in-memory state regardless.
        });
        await deps.onSessionInvalid();
        throw new ApiError({
          status: null,
          code: "TOKEN_STORAGE_ERROR",
          message: genericMessageFor("TOKEN_STORAGE_ERROR"),
          retryable: false,
        });
      }

      return tokens.access_token;
    })();

    try {
      return await inFlightRefresh;
    } finally {
      inFlightRefresh = null;
    }
  }

  async function authorizedRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const accessToken = await deps.tokenStorage.getAccessToken();

    if (accessToken === null) {
      const newAccessToken = await refreshOnce();
      return request<T>(path, withBearer(options, newAccessToken));
    }

    try {
      return await request<T>(path, withBearer(options, accessToken));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        const newAccessToken = await refreshOnce();
        return request<T>(path, withBearer(options, newAccessToken));
      }
      throw err;
    }
  }

  return { authorizedRequest, refreshOnce };
}

function withBearer(options: RequestOptions, accessToken: string): RequestOptions {
  return { ...options, headers: { ...options.headers, Authorization: `Bearer ${accessToken}` } };
}

export type RefreshCoordinator = ReturnType<typeof createRefreshCoordinator>;
