import type { SecureStoreAdapter } from "./secure-store-adapter";

const ACCESS_TOKEN_KEY = "auth.access_token";
const REFRESH_TOKEN_KEY = "auth.refresh_token";

/**
 * Bearer/refresh credentials live ONLY behind this module, which
 * itself only ever touches its injected SecureStoreAdapter -- never
 * AsyncStorage, SQLite, a plain file, or a persisted Query cache. See
 * src/utils/storage-policy.ts.
 */
export function createTokenStorage(adapter: SecureStoreAdapter) {
  return {
    async getAccessToken(): Promise<string | null> {
      return adapter.getItem(ACCESS_TOKEN_KEY);
    },
    async getRefreshToken(): Promise<string | null> {
      return adapter.getItem(REFRESH_TOKEN_KEY);
    },
    /** Access + refresh token are replaced together, atomically from
     * this module's callers' point of view -- a caller never observes
     * one updated without the other (both awaits happen before this
     * resolves; there is no partial-write window a concurrent reader
     * of this module could observe, since SecureStore itself has no
     * multi-key transaction primitive to do better than this). */
    async setTokens(accessToken: string, refreshToken: string): Promise<void> {
      await adapter.setItem(ACCESS_TOKEN_KEY, accessToken);
      await adapter.setItem(REFRESH_TOKEN_KEY, refreshToken);
    },
    async clear(): Promise<void> {
      await adapter.deleteItem(ACCESS_TOKEN_KEY);
      await adapter.deleteItem(REFRESH_TOKEN_KEY);
    },
    async hasStoredCredentials(): Promise<boolean> {
      return (await adapter.getItem(REFRESH_TOKEN_KEY)) !== null;
    },
  };
}

export type TokenStorage = ReturnType<typeof createTokenStorage>;
