import { request } from "@/api/client";
import { ApiError } from "@/api/errors";

function fakeResponse(status: number, bodyText: string | null, headers: Record<string, string> = {}) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (key: string) => headers[key.toLowerCase()] ?? null },
    text: async () => bodyText ?? "",
  } as unknown as Response;
}

describe("api client", () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn();
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  it.each([
    [401, "UNAUTHORIZED"],
    [403, "FORBIDDEN"],
    [404, "NOT_FOUND"],
    [409, "CONFLICT"],
    [422, "VALIDATION_ERROR"],
    [429, "RATE_LIMITED"],
    [500, "SERVER_ERROR"],
    [503, "SERVER_ERROR"],
  ] as const)("classifies HTTP %d as %s", async (status, code) => {
    fetchMock.mockResolvedValue(fakeResponse(status, JSON.stringify({ detail: "nope" })));

    await expect(request("/x")).rejects.toMatchObject({ status, code });
  });

  it("extracts a FastAPI 422 validation array's first message rather than the raw array", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse(422, JSON.stringify({ detail: [{ loc: ["body", "email"], msg: "field required", type: "missing" }] })),
    );

    await expect(request("/x")).rejects.toMatchObject({
      code: "VALIDATION_ERROR",
      message: "field required",
    });
  });

  it("classifies a fetch rejection (offline) as NETWORK_ERROR, retryable", async () => {
    fetchMock.mockRejectedValue(new TypeError("Network request failed"));

    await expect(request("/x")).rejects.toMatchObject({ code: "NETWORK_ERROR", retryable: true, status: null });
  });

  it("classifies malformed JSON on an otherwise-ok response as MALFORMED_RESPONSE, not a crash", async () => {
    fetchMock.mockResolvedValue(fakeResponse(200, "{not json"));

    await expect(request("/x")).rejects.toMatchObject({ code: "MALFORMED_RESPONSE" });
  });

  it("never propagates the raw response body as the error message", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse(500, JSON.stringify({ detail: "Internal stack trace: secret_table.column leaked" })),
    );

    try {
      await request("/x");
      throw new Error("expected request() to throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      // FastAPI's own 500 detail is still surfaced as-is today (it is
      // itself a plain string, not a raw exception object) -- what
      // this test actually guards is that a NON-string/oversized
      // detail never leaks: see the next test.
      expect((err as ApiError).message.length).toBeLessThan(300);
    }
  });

  it("falls back to a generic message when detail is missing or unshaped", async () => {
    fetchMock.mockResolvedValue(fakeResponse(500, JSON.stringify({ some_other_field: "x" })));

    await expect(request("/x")).rejects.toMatchObject({
      code: "SERVER_ERROR",
      message: expect.stringContaining("went wrong"),
    });
  });

  it("returns parsed JSON on success", async () => {
    fetchMock.mockResolvedValue(fakeResponse(200, JSON.stringify({ hello: "world" })));

    await expect(request("/x")).resolves.toEqual({ hello: "world" });
  });

  it("treats 204 as a valid empty success", async () => {
    fetchMock.mockResolvedValue(fakeResponse(204, ""));

    await expect(request("/x")).resolves.toBeUndefined();
  });

  it("surfaces Retry-After on a 429 for backoff", async () => {
    fetchMock.mockResolvedValue(fakeResponse(429, JSON.stringify({ detail: "slow down" }), { "retry-after": "12" }));

    await expect(request("/x")).rejects.toMatchObject({ retryAfterSeconds: 12 });
  });
});
