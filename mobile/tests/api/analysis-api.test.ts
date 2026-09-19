import { ApiError } from "@/api/errors";
import { getAnalysis, getAnalysisHistory, submitAnalysis } from "@/api/analysis-api";
import { authorizedRequest } from "@/auth/auth-client-singleton";

jest.mock("@/auth/auth-client-singleton");

const mockAuthorizedRequest = authorizedRequest as jest.Mock;

describe("analysis-api", () => {
  beforeEach(() => {
    mockAuthorizedRequest.mockReset();
  });

  it("submitAnalysis() posts to /api/v2/analyses with the image and request_id", async () => {
    const response = { analysis_id: "a1", request_id: "r1", status: "QUEUED" };
    mockAuthorizedRequest.mockResolvedValue(response);

    await expect(submitAnalysis({ image_base64: "abc123", request_id: "r1" })).resolves.toEqual(response);
    expect(mockAuthorizedRequest).toHaveBeenCalledWith("/api/v2/analyses", {
      method: "POST",
      body: { image_base64: "abc123", request_id: "r1" },
    });
  });

  it("getAnalysis() reads GET /api/v2/analyses/{id}", async () => {
    const response = { analysis_id: "a1", request_id: "r1", status: "PROCESSING" };
    mockAuthorizedRequest.mockResolvedValue(response);

    await expect(getAnalysis("a1")).resolves.toEqual(response);
    expect(mockAuthorizedRequest).toHaveBeenCalledWith("/api/v2/analyses/a1");
  });

  it.each([
    ["consent denied", 403, "FORBIDDEN" as const],
    ["image validation failed", 422, "VALIDATION_ERROR" as const],
    ["quota exceeded", 429, "RATE_LIMITED" as const],
    ["backend failure", 500, "SERVER_ERROR" as const],
  ])("submitAnalysis() propagates a %s error untouched for the caller to classify", async (_label, status, code) => {
    const error = new ApiError({ status, code, message: "x", retryable: status >= 500 || status === 429 });
    mockAuthorizedRequest.mockRejectedValue(error);

    await expect(submitAnalysis({ image_base64: "abc", request_id: "r1" })).rejects.toBe(error);
  });

  it("submitAnalysis() propagates a network error untouched", async () => {
    const error = new ApiError({ status: null, code: "NETWORK_ERROR", message: "offline", retryable: true });
    mockAuthorizedRequest.mockRejectedValue(error);

    await expect(submitAnalysis({ image_base64: "abc", request_id: "r1" })).rejects.toBe(error);
  });

  it("getAnalysis() propagates a not-found error untouched", async () => {
    const error = new ApiError({ status: 404, code: "NOT_FOUND", message: "Analysis not found", retryable: false });
    mockAuthorizedRequest.mockRejectedValue(error);

    await expect(getAnalysis("missing")).rejects.toBe(error);
  });

  it("getAnalysisHistory() reads GET /api/v2/analyses with no query string on the first page", async () => {
    const response = { items: [], next_cursor: null };
    mockAuthorizedRequest.mockResolvedValue(response);

    await expect(getAnalysisHistory()).resolves.toEqual(response);
    expect(mockAuthorizedRequest).toHaveBeenCalledWith("/api/v2/analyses");
  });

  it("getAnalysisHistory() echoes an opaque cursor back verbatim, URL-encoded", async () => {
    const response = { items: [], next_cursor: null };
    mockAuthorizedRequest.mockResolvedValue(response);

    await getAnalysisHistory({ cursor: "abc+def/==" });
    expect(mockAuthorizedRequest).toHaveBeenCalledWith(`/api/v2/analyses?cursor=${encodeURIComponent("abc+def/==")}`);
  });

  it("getAnalysisHistory() propagates a backend error untouched", async () => {
    const error = new ApiError({ status: 400, code: "VALIDATION_ERROR", message: "Invalid cursor", retryable: false });
    mockAuthorizedRequest.mockRejectedValue(error);

    await expect(getAnalysisHistory({ cursor: "bad" })).rejects.toBe(error);
  });
});
