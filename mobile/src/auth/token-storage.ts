import type { SecureStoreAdapter } from "./secure-store-adapter";

const TOKEN_PAIR_KEY = "auth.token_pair";
// Pre-public-beta legacy keys (mobile V1 foundation's original two-key
// scheme). Removed on every clear() so a dev install that still has
// them from an earlier build never retains obsolete credentials --
// see this module's own docstring below for why the two-key scheme
// itself was replaced.
const LEGACY_ACCESS_TOKEN_KEY = "auth.access_token";
const LEGACY_REFRESH_TOKEN_KEY = "auth.refresh_token";

const TOKEN_PAIR_VERSION = 1 as const;

export type TokenPair = {
  access_token: string;
  refresh_token: string;
};

type StoredTokenPair = TokenPair & { version: typeof TOKEN_PAIR_VERSION };

function isStoredTokenPair(value: unknown): value is StoredTokenPair {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<StoredTokenPair>;
  return (
    candidate.version === TOKEN_PAIR_VERSION &&
    typeof candidate.access_token === "string" &&
    typeof candidate.refresh_token === "string"
  );
}

/**
 * Bearer/refresh credentials live ONLY behind this module, which
 * itself only ever touches its injected SecureStoreAdapter -- never
 * AsyncStorage, SQLite, a plain file, or a persisted Query cache. See
 * src/utils/storage-policy.ts.
 *
 * Access + refresh are stored as ONE versioned JSON value under a
 * single SecureStore key (mobile V1 repair pass -- previously two
 * independent keys). That mattered because a second-write failure
 * across two separate keys could leave a NEW access token paired with
 * the OLD refresh token, and the backend's refresh-rotation contract
 * makes that specifically dangerous: the old refresh token was already
 * consumed server-side during the rotation that just produced the new
 * pair, and reusing a consumed single-use refresh token triggers the
 * backend's replay-family revocation (backend/app/main.py's /refresh
 * docstring). Writing one JSON value means one adapter.setItem call --
 * there is no window between "access written" and "refresh written"
 * for a reader (or a crash) to observe a torn pair in.
 */
export function createTokenStorage(adapter: SecureStoreAdapter) {
  async function readPair(): Promise<TokenPair | null> {
    const raw = await adapter.getItem(TOKEN_PAIR_KEY);
    if (raw === null) return null;
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return null;
    }
    return isStoredTokenPair(parsed) ? { access_token: parsed.access_token, refresh_token: parsed.refresh_token } : null;
  }

  return {
    async getTokenPair(): Promise<TokenPair | null> {
      return readPair();
    },
    /** Convenience accessor -- reads the same single stored pair as
     * getTokenPair(), never a separate value. */
    async getAccessToken(): Promise<string | null> {
      const pair = await readPair();
      return pair?.access_token ?? null;
    },
    /** Convenience accessor -- reads the same single stored pair as
     * getTokenPair(), never a separate value. */
    async getRefreshToken(): Promise<string | null> {
      const pair = await readPair();
      return pair?.refresh_token ?? null;
    },
    async setTokenPair(tokens: TokenPair): Promise<void> {
      const stored: StoredTokenPair = { version: TOKEN_PAIR_VERSION, ...tokens };
      await adapter.setItem(TOKEN_PAIR_KEY, JSON.stringify(stored));
    },
    async clear(): Promise<void> {
      await adapter.deleteItem(TOKEN_PAIR_KEY);
      await adapter.deleteItem(LEGACY_ACCESS_TOKEN_KEY);
      await adapter.deleteItem(LEGACY_REFRESH_TOKEN_KEY);
    },
    async hasStoredCredentials(): Promise<boolean> {
      return (await readPair()) !== null;
    },
  };
}

export type TokenStorage = ReturnType<typeof createTokenStorage>;
