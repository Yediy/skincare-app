import type { TokenStorage } from "./token-storage";

export type SessionState =
  | { status: "UNKNOWN" }
  | { status: "SIGNED_OUT" }
  | { status: "AUTHENTICATED" };

/**
 * Deliberately does exactly one thing: read whether a refresh token
 * exists in SecureStore. No network call. That's what makes bootstrap
 * fast and offline-safe -- it can never hang waiting for a dead
 * network at launch, and "no network at startup" can never look like
 * "no credentials" (section 22).
 *
 * This optimistically trusts a stored refresh token: if the access
 * token has actually expired, or the refresh token has actually been
 * revoked, that surfaces the first time an authenticated query runs
 * (e.g. the app-group layout's own bootstrap query) and 401s -- which
 * goes through the exact same single-flight refresh-coordinator used
 * by every other authenticated call (see refresh-coordinator.ts), not
 * a separate bootstrap-specific refresh path.
 */
export async function restoreSession(tokenStorage: TokenStorage): Promise<SessionState> {
  const hasCredentials = await tokenStorage.hasStoredCredentials();
  return hasCredentials ? { status: "AUTHENTICATED" } : { status: "SIGNED_OUT" };
}
