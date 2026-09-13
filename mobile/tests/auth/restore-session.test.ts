import { createInMemorySecureStoreAdapter } from "@/auth/secure-store-adapter";
import { restoreSession } from "@/auth/restore-session";
import { createTokenStorage } from "@/auth/token-storage";

describe("restoreSession", () => {
  it("resolves SIGNED_OUT when no refresh token is stored", async () => {
    const storage = createTokenStorage(createInMemorySecureStoreAdapter());
    const result = await restoreSession(storage);
    expect(result).toEqual({ status: "SIGNED_OUT" });
  });

  it("resolves AUTHENTICATED when a refresh token is stored", async () => {
    const storage = createTokenStorage(createInMemorySecureStoreAdapter());
    await storage.setTokens("access-1", "refresh-1");

    const result = await restoreSession(storage);
    expect(result).toEqual({ status: "AUTHENTICATED" });
  });

  it("never touches the network -- purely a local SecureStore read", async () => {
    const fetchSpy = jest.spyOn(global, "fetch");
    const storage = createTokenStorage(createInMemorySecureStoreAdapter());
    await storage.setTokens("access-1", "refresh-1");

    await restoreSession(storage);

    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });
});
