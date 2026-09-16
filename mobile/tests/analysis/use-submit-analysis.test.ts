import { ApiError } from "@/api/errors";
import { submitAnalysis } from "@/api/analysis-api";
import { deleteLocalCaptureFile, prepareCaptureForSubmission } from "@/analysis/capture-file";
import { submitCapture } from "@/analysis/use-submit-analysis";

jest.mock("@/api/analysis-api");
jest.mock("@/analysis/capture-file");

const mockSubmitAnalysis = submitAnalysis as jest.Mock;
const mockPrepare = prepareCaptureForSubmission as jest.Mock;
const mockDelete = deleteLocalCaptureFile as jest.Mock;

const BASE_INPUT = {
  capturedImageUri: "file:///cache/original.jpg",
  capturedImageWidth: 4000,
  capturedImageHeight: 3000,
  requestId: "r1",
};

describe("submitCapture", () => {
  beforeEach(() => {
    mockSubmitAnalysis.mockReset();
    mockPrepare.mockReset();
    mockDelete.mockReset();
  });

  it("prepares the capture, submits with the given request_id, then deletes both temp files on success", async () => {
    mockPrepare.mockResolvedValue({ uri: "file:///cache/resized.jpg", base64: "B64", width: 1600, height: 1200 });
    mockSubmitAnalysis.mockResolvedValue({ analysis_id: "a1", request_id: "r1", status: "QUEUED" });

    const result = await submitCapture(BASE_INPUT);

    expect(result).toEqual({ analysis_id: "a1", request_id: "r1", status: "QUEUED" });
    expect(mockPrepare).toHaveBeenCalledWith("file:///cache/original.jpg", 4000, 3000);
    expect(mockSubmitAnalysis).toHaveBeenCalledWith({ image_base64: "B64", request_id: "r1" });
    expect(mockDelete).toHaveBeenCalledWith("file:///cache/resized.jpg");
    expect(mockDelete).toHaveBeenCalledWith("file:///cache/original.jpg");
    expect(mockDelete).toHaveBeenCalledTimes(2);
  });

  it("on a transient submission failure, deletes the PREPARED working copy but retains the ORIGINAL capture for retry", async () => {
    mockPrepare.mockResolvedValue({ uri: "file:///cache/resized.jpg", base64: "B64", width: 1600, height: 1200 });
    const error = new ApiError({ status: 500, code: "SERVER_ERROR", message: "x", retryable: true });
    mockSubmitAnalysis.mockRejectedValue(error);

    await expect(submitCapture(BASE_INPUT)).rejects.toBe(error);

    expect(mockDelete).toHaveBeenCalledWith("file:///cache/resized.jpg");
    expect(mockDelete).toHaveBeenCalledTimes(1);
    expect(mockDelete).not.toHaveBeenCalledWith("file:///cache/original.jpg");
  });

  it("multiple failed retries never accumulate abandoned prepared copies -- each attempt's own working file is deleted", async () => {
    mockPrepare
      .mockResolvedValueOnce({ uri: "file:///cache/resized-1.jpg", base64: "B64", width: 1600, height: 1200 })
      .mockResolvedValueOnce({ uri: "file:///cache/resized-2.jpg", base64: "B64", width: 1600, height: 1200 })
      .mockResolvedValueOnce({ uri: "file:///cache/resized-3.jpg", base64: "B64", width: 1600, height: 1200 });
    const error = new ApiError({ status: null, code: "NETWORK_ERROR", message: "offline", retryable: true });
    mockSubmitAnalysis.mockRejectedValue(error);

    await expect(submitCapture(BASE_INPUT)).rejects.toBe(error);
    await expect(submitCapture(BASE_INPUT)).rejects.toBe(error);
    await expect(submitCapture(BASE_INPUT)).rejects.toBe(error);

    expect(mockDelete).toHaveBeenCalledWith("file:///cache/resized-1.jpg");
    expect(mockDelete).toHaveBeenCalledWith("file:///cache/resized-2.jpg");
    expect(mockDelete).toHaveBeenCalledWith("file:///cache/resized-3.jpg");
    expect(mockDelete).toHaveBeenCalledTimes(3);
    expect(mockDelete).not.toHaveBeenCalledWith("file:///cache/original.jpg");
  });

  it("reuses the exact same request_id across a retry of the same attempt", async () => {
    mockPrepare.mockResolvedValue({ uri: "file:///cache/resized.jpg", base64: "B64", width: 1600, height: 1200 });
    mockSubmitAnalysis
      .mockRejectedValueOnce(new ApiError({ status: null, code: "NETWORK_ERROR", message: "offline", retryable: true }))
      .mockResolvedValueOnce({ analysis_id: "a1", request_id: "r1", status: "QUEUED" });

    await expect(submitCapture(BASE_INPUT)).rejects.toBeInstanceOf(ApiError);

    await submitCapture(BASE_INPUT);

    expect(mockSubmitAnalysis).toHaveBeenNthCalledWith(1, { image_base64: "B64", request_id: "r1" });
    expect(mockSubmitAnalysis).toHaveBeenNthCalledWith(2, { image_base64: "B64", request_id: "r1" });
  });
});
