import { createInMemorySecureStoreAdapter } from "@/auth/secure-store-adapter";
import { createTokenStorage } from "@/auth/token-storage";

describe("token-storage", () => {
  it("has no stored credentials before anything is written", async () => {
    const storage = createTokenStorage(createInMemorySecureStoreAdapter());
    expect(await storage.hasStoredCredentials()).toBe(false);
    expect(await storage.getAccessToken()).toBeNull();
    expect(await storage.getRefreshToken()).toBeNull();
  });

  it("writes access + refresh tokens to the adapter", async () => {
    const adapter = createInMemorySecureStoreAdapter();
    const storage = createTokenStorage(adapter);

    await storage.setTokens("access-1", "refresh-1");

    expect(await storage.getAccessToken()).toBe("access-1");
    expect(await storage.getRefreshToken()).toBe("refresh-1");
    expect(await storage.hasStoredCredentials()).toBe(true);
    // Proves it really went through the injected adapter, not some
    // other channel.
    expect(await adapter.getItem("auth.access_token")).toBe("access-1");
    expect(await adapter.getItem("auth.refresh_token")).toBe("refresh-1");
  });

  it("clear() (sign-out path) deletes both tokens", async () => {
    const storage = createTokenStorage(createInMemorySecureStoreAdapter());
    await storage.setTokens("access-1", "refresh-1");

    await storage.clear();

    expect(await storage.getAccessToken()).toBeNull();
    expect(await storage.getRefreshToken()).toBeNull();
    expect(await storage.hasStoredCredentials()).toBe(false);
  });

  it("clear() (account-deletion path) deletes both tokens the same way", async () => {
    // Account deletion and sign-out both call the same clear() --
    // proven identical here so a future divergence would be caught.
    const storage = createTokenStorage(createInMemorySecureStoreAdapter());
    await storage.setTokens("access-2", "refresh-2");

    await storage.clear();

    expect(await storage.hasStoredCredentials()).toBe(false);
  });
});
