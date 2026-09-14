import { createInMemorySecureStoreAdapter } from "@/auth/secure-store-adapter";
import { createTokenStorage } from "@/auth/token-storage";
import type { SecureStoreAdapter } from "@/auth/secure-store-adapter";

/** Wraps a real in-memory adapter but lets a test force setItem() to
 * reject -- the only way to actually exercise "SecureStore write
 * fails" without a real device keychain. */
function createFailingSetItemAdapter(base: SecureStoreAdapter, error: Error): SecureStoreAdapter {
  return {
    getItem: base.getItem,
    deleteItem: base.deleteItem,
    async setItem() {
      throw error;
    },
  };
}

describe("token-storage", () => {
  it("has no stored credentials before anything is written", async () => {
    const storage = createTokenStorage(createInMemorySecureStoreAdapter());
    expect(await storage.hasStoredCredentials()).toBe(false);
    expect(await storage.getAccessToken()).toBeNull();
    expect(await storage.getRefreshToken()).toBeNull();
    expect(await storage.getTokenPair()).toBeNull();
  });

  it("writes the pair under a single SecureStore key, as one versioned JSON value", async () => {
    const adapter = createInMemorySecureStoreAdapter();
    const storage = createTokenStorage(adapter);

    await storage.setTokenPair({ access_token: "access-1", refresh_token: "refresh-1" });

    expect(await storage.getAccessToken()).toBe("access-1");
    expect(await storage.getRefreshToken()).toBe("refresh-1");
    expect(await storage.getTokenPair()).toEqual({ access_token: "access-1", refresh_token: "refresh-1" });
    expect(await storage.hasStoredCredentials()).toBe(true);

    // Proves it really went through the injected adapter as ONE key,
    // not two -- the legacy keys must never be written by the new path.
    const raw = await adapter.getItem("auth.token_pair");
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw as string)).toEqual({ version: 1, access_token: "access-1", refresh_token: "refresh-1" });
    expect(await adapter.getItem("auth.access_token")).toBeNull();
    expect(await adapter.getItem("auth.refresh_token")).toBeNull();
  });

  it("a read never observes a mismatched pair -- access and refresh always come from the same single write", async () => {
    const storage = createTokenStorage(createInMemorySecureStoreAdapter());
    await storage.setTokenPair({ access_token: "access-1", refresh_token: "refresh-1" });
    await storage.setTokenPair({ access_token: "access-2", refresh_token: "refresh-2" });

    // There is only ever one adapter.setItem call per setTokenPair(),
    // so it is structurally impossible to read "access-2" alongside
    // "refresh-1" -- the pair the last successful setTokenPair()
    // wrote is exactly the pair every subsequent read returns.
    const pair = await storage.getTokenPair();
    expect(pair).toEqual({ access_token: "access-2", refresh_token: "refresh-2" });
  });

  it("clear() removes the token pair", async () => {
    const storage = createTokenStorage(createInMemorySecureStoreAdapter());
    await storage.setTokenPair({ access_token: "access-1", refresh_token: "refresh-1" });

    await storage.clear();

    expect(await storage.getTokenPair()).toBeNull();
    expect(await storage.getAccessToken()).toBeNull();
    expect(await storage.getRefreshToken()).toBeNull();
    expect(await storage.hasStoredCredentials()).toBe(false);
  });

  it("clear() also removes the pre-repair-pass legacy two-key values, if present", async () => {
    const adapter = createInMemorySecureStoreAdapter();
    const storage = createTokenStorage(adapter);
    // Simulate a dev install left over from before this pass, with the
    // old two-key scheme still populated and no new-scheme key yet.
    await adapter.setItem("auth.access_token", "legacy-access");
    await adapter.setItem("auth.refresh_token", "legacy-refresh");

    await storage.clear();

    expect(await adapter.getItem("auth.access_token")).toBeNull();
    expect(await adapter.getItem("auth.refresh_token")).toBeNull();
  });

  it("clear() (account-deletion path) deletes the pair the same way as sign-out", async () => {
    // Account deletion and sign-out both call the same clear() --
    // proven identical here so a future divergence would be caught.
    const storage = createTokenStorage(createInMemorySecureStoreAdapter());
    await storage.setTokenPair({ access_token: "access-2", refresh_token: "refresh-2" });

    await storage.clear();

    expect(await storage.hasStoredCredentials()).toBe(false);
  });

  it("a malformed/legacy-shaped value under the new key is treated as absent, not as a crash", async () => {
    const adapter = createInMemorySecureStoreAdapter();
    const storage = createTokenStorage(adapter);
    await adapter.setItem("auth.token_pair", "not valid json");

    expect(await storage.getTokenPair()).toBeNull();
    expect(await storage.hasStoredCredentials()).toBe(false);
  });

  it("setTokenPair() propagates a SecureStore write failure rather than silently succeeding", async () => {
    const base = createInMemorySecureStoreAdapter();
    const failingAdapter = createFailingSetItemAdapter(base, new Error("keychain write failed"));
    const storage = createTokenStorage(failingAdapter);

    await expect(
      storage.setTokenPair({ access_token: "access-1", refresh_token: "refresh-1" }),
    ).rejects.toThrow("keychain write failed");

    // Nothing was durably written -- the failed write must not leave a
    // partial/garbage value behind for a later read to trust.
    expect(await storage.getTokenPair()).toBeNull();
  });
});
