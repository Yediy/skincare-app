import { ApiError } from "@/api/errors";
import { createRefreshCoordinator } from "@/auth/refresh-coordinator";
import { createInMemorySecureStoreAdapter } from "@/auth/secure-store-adapter";
import { createTokenStorage } from "@/auth/token-storage";

function fakeResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  const text = JSON.stringify(body);
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (key: string) => headers[key.toLowerCase()] ?? null },
    text: async () => text,
  } as unknown as Response;
}

function authHeader(init?: RequestInit): string | undefined {
  const headers = init?.headers as Record<string, string> | undefined;
  return headers?.Authorization;
}

describe("refresh-coordinator", () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn();
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  it("attaches the current access token and returns on success without ever refreshing", async () => {
    const tokenStorage = createTokenStorage(createInMemorySecureStoreAdapter());
    await tokenStorage.setTokenPair({ access_token: "good-access", refresh_token: "refresh-1" });
    const refreshTokens = jest.fn();
    const onSessionInvalid = jest.fn();
    const { authorizedRequest } = createRefreshCoordinator({ tokenStorage, refreshTokens, onSessionInvalid });

    fetchMock.mockImplementation(async () => fakeResponse(200, { ok: true }));

    const result = await authorizedRequest<{ ok: boolean }>("/thing");

    expect(result).toEqual({ ok: true });
    expect(refreshTokens).not.toHaveBeenCalled();
    expect(onSessionInvalid).not.toHaveBeenCalled();
  });

  it("expired access token: exactly one refresh, then retries the original request once", async () => {
    const tokenStorage = createTokenStorage(createInMemorySecureStoreAdapter());
    await tokenStorage.setTokenPair({ access_token: "old-access", refresh_token: "refresh-1" });
    const refreshTokens = jest.fn(async () => ({
      access_token: "new-access",
      refresh_token: "new-refresh",
      token_type: "bearer" as const,
    }));
    const onSessionInvalid = jest.fn();
    const { authorizedRequest } = createRefreshCoordinator({ tokenStorage, refreshTokens, onSessionInvalid });

    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (authHeader(init) === "Bearer old-access") return fakeResponse(401, { detail: "expired" });
      if (authHeader(init) === "Bearer new-access") return fakeResponse(200, { ok: true });
      throw new Error("unexpected request");
    });

    const result = await authorizedRequest<{ ok: boolean }>("/thing");

    expect(result).toEqual({ ok: true });
    expect(refreshTokens).toHaveBeenCalledTimes(1);
    expect(refreshTokens).toHaveBeenCalledWith("refresh-1");
    expect(await tokenStorage.getAccessToken()).toBe("new-access");
    expect(await tokenStorage.getRefreshToken()).toBe("new-refresh");
    expect(onSessionInvalid).not.toHaveBeenCalled();
  });

  it("refresh concurrency: N simultaneous 401s trigger exactly one refresh call, and every caller resolves with the new access token's response", async () => {
    const tokenStorage = createTokenStorage(createInMemorySecureStoreAdapter());
    await tokenStorage.setTokenPair({ access_token: "old-access", refresh_token: "refresh-1" });
    let refreshCallCount = 0;
    const refreshTokens = jest.fn(async () => {
      refreshCallCount += 1;
      // Simulate real async latency so concurrent callers actually
      // overlap in time rather than resolving synchronously.
      await new Promise((resolve) => setTimeout(resolve, 5));
      return { access_token: "new-access", refresh_token: "new-refresh", token_type: "bearer" as const };
    });
    const onSessionInvalid = jest.fn();
    const { authorizedRequest } = createRefreshCoordinator({ tokenStorage, refreshTokens, onSessionInvalid });

    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      const auth = authHeader(init);
      if (auth === "Bearer old-access") return fakeResponse(401, { detail: "expired" });
      if (auth === "Bearer new-access") return fakeResponse(200, { ok: true });
      throw new Error(`unexpected auth header: ${auth}`);
    });

    const CONCURRENT_CALLS = 8;
    const results = await Promise.all(
      Array.from({ length: CONCURRENT_CALLS }, () => authorizedRequest<{ ok: boolean }>("/thing")),
    );

    expect(results).toEqual(Array.from({ length: CONCURRENT_CALLS }, () => ({ ok: true })));
    // The one requirement this test exists to prove: never one
    // /refresh call per failed request.
    expect(refreshCallCount).toBe(1);
    expect(refreshTokens).toHaveBeenCalledTimes(1);
  });

  it("refresh token rejected by the backend (401/403): clears storage and signals session-invalid, never loops", async () => {
    const tokenStorage = createTokenStorage(createInMemorySecureStoreAdapter());
    await tokenStorage.setTokenPair({ access_token: "old-access", refresh_token: "revoked-refresh" });
    const refreshTokens = jest.fn(async () => {
      throw new ApiError({ status: 401, code: "UNAUTHORIZED", message: "invalid refresh token", retryable: false });
    });
    const onSessionInvalid = jest.fn();
    const { authorizedRequest } = createRefreshCoordinator({ tokenStorage, refreshTokens, onSessionInvalid });

    fetchMock.mockImplementation(async () => fakeResponse(401, { detail: "expired" }));

    await expect(authorizedRequest("/thing")).rejects.toBeInstanceOf(ApiError);

    expect(refreshTokens).toHaveBeenCalledTimes(1);
    expect(onSessionInvalid).toHaveBeenCalledTimes(1);
    expect(await tokenStorage.getAccessToken()).toBeNull();
    expect(await tokenStorage.getRefreshToken()).toBeNull();
  });

  it("network/server failure during refresh itself does NOT end the session (offline must never look like an invalid account)", async () => {
    const tokenStorage = createTokenStorage(createInMemorySecureStoreAdapter());
    await tokenStorage.setTokenPair({ access_token: "old-access", refresh_token: "refresh-1" });
    const refreshTokens = jest.fn(async () => {
      throw new ApiError({ status: null, code: "NETWORK_ERROR", message: "offline", retryable: true });
    });
    const onSessionInvalid = jest.fn();
    const { authorizedRequest } = createRefreshCoordinator({ tokenStorage, refreshTokens, onSessionInvalid });

    fetchMock.mockImplementation(async () => fakeResponse(401, { detail: "expired" }));

    await expect(authorizedRequest("/thing")).rejects.toBeInstanceOf(ApiError);

    expect(onSessionInvalid).not.toHaveBeenCalled();
    // The (still possibly valid) refresh token is preserved -- a
    // transient network failure must not throw away a good session.
    expect(await tokenStorage.getRefreshToken()).toBe("refresh-1");
  });

  it("no refresh token stored at all: fails closed without ever calling the network refresh endpoint", async () => {
    const tokenStorage = createTokenStorage(createInMemorySecureStoreAdapter());
    // No setTokens() call -- simulates an access token surviving in
    // memory with no refresh token backing it, an inconsistent state
    // that must never be trusted.
    const refreshTokens = jest.fn();
    const onSessionInvalid = jest.fn();
    const { authorizedRequest } = createRefreshCoordinator({ tokenStorage, refreshTokens, onSessionInvalid });

    await expect(authorizedRequest("/thing")).rejects.toBeInstanceOf(ApiError);
    expect(refreshTokens).not.toHaveBeenCalled();
    expect(onSessionInvalid).toHaveBeenCalledTimes(1);
  });

  it("never retries more than once: a 401 on the post-refresh retry itself is not refreshed again", async () => {
    const tokenStorage = createTokenStorage(createInMemorySecureStoreAdapter());
    await tokenStorage.setTokenPair({ access_token: "old-access", refresh_token: "refresh-1" });
    const refreshTokens = jest.fn(async () => ({
      access_token: "new-access",
      refresh_token: "new-refresh",
      token_type: "bearer" as const,
    }));
    const onSessionInvalid = jest.fn();
    const { authorizedRequest } = createRefreshCoordinator({ tokenStorage, refreshTokens, onSessionInvalid });

    // Every request 401s, even with the "new" token -- if this module
    // looped, refreshTokens would be called more than once.
    fetchMock.mockImplementation(async () => fakeResponse(401, { detail: "still expired" }));

    await expect(authorizedRequest("/thing")).rejects.toMatchObject({ status: 401 });
    expect(refreshTokens).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2); // original attempt + exactly one retry
  });

  it(
    "storage-write failure AFTER a successful server refresh fails closed: the old (already-consumed) " +
      "refresh token is never retried, storage is best-effort cleared, and the session ends",
    async () => {
      const realStorage = createTokenStorage(createInMemorySecureStoreAdapter());
      await realStorage.setTokenPair({ access_token: "old-access", refresh_token: "refresh-1" });
      const clearSpy = jest.spyOn(realStorage, "clear");
      const tokenStorage = {
        ...realStorage,
        setTokenPair: jest.fn(async () => {
          throw new Error("keychain write failed");
        }),
      };
      const refreshTokens = jest.fn(async () => ({
        access_token: "new-access",
        refresh_token: "new-refresh",
        token_type: "bearer" as const,
      }));
      const onSessionInvalid = jest.fn();
      const { authorizedRequest } = createRefreshCoordinator({ tokenStorage, refreshTokens, onSessionInvalid });

      fetchMock.mockImplementation(async () => fakeResponse(401, { detail: "expired" }));

      const error = await authorizedRequest("/thing").catch((e) => e);

      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).code).toBe("TOKEN_STORAGE_ERROR");
      // The server-issued new pair was never persisted, so the ONLY
      // request the mock fetch should ever see is the original attempt
      // with the old access token -- the coordinator must not attempt
      // to retry the original request with a token it never durably
      // stored, nor fall back to the old (already server-side-consumed)
      // refresh token.
      expect(refreshTokens).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(clearSpy).toHaveBeenCalledTimes(1);
      expect(onSessionInvalid).toHaveBeenCalledTimes(1);
    },
  );

  it("refresh concurrency at higher volume: 10 simultaneous 401s -> exactly one refresh call, one token-pair persistence, and every waiter gets the same new access token", async () => {
    const tokenStorage = createTokenStorage(createInMemorySecureStoreAdapter());
    await tokenStorage.setTokenPair({ access_token: "old-access", refresh_token: "refresh-1" });
    const setTokenPairSpy = jest.spyOn(tokenStorage, "setTokenPair");
    let refreshCallCount = 0;
    const refreshTokens = jest.fn(async () => {
      refreshCallCount += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return { access_token: "new-access", refresh_token: "new-refresh", token_type: "bearer" as const };
    });
    const onSessionInvalid = jest.fn();
    const { authorizedRequest } = createRefreshCoordinator({ tokenStorage, refreshTokens, onSessionInvalid });

    const seenAuthHeaders: string[] = [];
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      const auth = authHeader(init);
      if (auth) seenAuthHeaders.push(auth);
      if (auth === "Bearer old-access") return fakeResponse(401, { detail: "expired" });
      if (auth === "Bearer new-access") return fakeResponse(200, { ok: true });
      throw new Error(`unexpected auth header: ${auth}`);
    });

    const CONCURRENT_CALLS = 10;
    const results = await Promise.all(
      Array.from({ length: CONCURRENT_CALLS }, () => authorizedRequest<{ ok: boolean }>("/thing")),
    );

    expect(results).toEqual(Array.from({ length: CONCURRENT_CALLS }, () => ({ ok: true })));
    expect(refreshCallCount).toBe(1);
    expect(refreshTokens).toHaveBeenCalledTimes(1);
    expect(setTokenPairSpy).toHaveBeenCalledTimes(1);
    expect(setTokenPairSpy).toHaveBeenCalledWith(
      expect.objectContaining({ access_token: "new-access", refresh_token: "new-refresh" }),
    );
    // Every successful retry used the same new access token -- never a
    // mix, which would indicate more than one refresh/persist round
    // happened under concurrent load.
    expect(seenAuthHeaders.filter((h) => h === "Bearer new-access")).toHaveLength(CONCURRENT_CALLS);
  });
});
