/**
 * Storage policy (Mobile V1 foundation). This module is deliberately
 * mostly documentation -- the enforcement is in what call sites
 * *don't* do (no AsyncStorage/SQLite import anywhere near auth code),
 * not in a runtime guard. Read this before adding any new persisted
 * field.
 *
 * ALLOWED:
 *  - Auth credentials (access token, refresh token) -> expo-secure-store,
 *    exclusively through src/auth/secure-store-adapter.ts.
 *  - Non-sensitive UI preferences (e.g. "has seen welcome screen") ->
 *    a plain local store (AsyncStorage or similar) would be fine for
 *    these, but this pass introduces none -- there is nothing yet
 *    that needs it.
 *
 * NOT ALLOWED, ANYWHERE IN THIS APP:
 *  - Raw facial photos or analysis photo base64 data. Phase B owns
 *    the ephemeral capture pipeline; this PR does not add any image
 *    storage, full stop.
 *  - Bearer/refresh tokens in AsyncStorage, SQLite, a plain JSON file,
 *    or React Query's cache persisted to disk.
 *  - Unredacted API responses containing sensitive profile
 *    information (allergies, pregnancy/nursing, avoid_ingredients)
 *    written to any persistent store. TanStack Query's in-memory
 *    cache holds these transiently for the running session only --
 *    this pass does not persist the Query cache to disk at all (see
 *    src/query/query-client.ts).
 *  - Any of the above in a console.log / logger call -- see
 *    src/utils/logger.ts's redaction list.
 */
export const STORAGE_POLICY_NOTE =
  "Auth credentials live only in expo-secure-store, via src/auth/secure-store-adapter.ts. " +
  "No token, password, image, or sensitive profile field is ever written to AsyncStorage, " +
  "SQLite, a plain file, a persisted Query cache, or a log line.";
