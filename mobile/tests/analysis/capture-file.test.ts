import { File } from "expo-file-system";
import { manipulateAsync } from "expo-image-manipulator";

import { deleteLocalCaptureFile, prepareCaptureForSubmission } from "@/analysis/capture-file";
import { logger } from "@/utils/logger";

jest.mock("expo-file-system", () => ({ File: jest.fn() }));
jest.mock("expo-image-manipulator", () => ({
  manipulateAsync: jest.fn(),
  SaveFormat: { JPEG: "jpeg" },
}));
jest.mock("@/utils/logger", () => ({ logger: { warn: jest.fn(), error: jest.fn(), debug: jest.fn() } }));

const mockManipulateAsync = manipulateAsync as jest.Mock;
const MockFile = File as unknown as jest.Mock;

describe("prepareCaptureForSubmission", () => {
  beforeEach(() => {
    mockManipulateAsync.mockReset();
    MockFile.mockReset();
    MockFile.mockImplementation(() => ({ exists: true, delete: jest.fn() }));
    (logger.warn as jest.Mock).mockReset();
  });

  it("resizes/compresses and returns the resulting base64, never logging it", async () => {
    mockManipulateAsync.mockResolvedValue({ uri: "file:///cache/resized.jpg", base64: "BASE64DATA", width: 1600, height: 1200 });

    const result = await prepareCaptureForSubmission("file:///cache/original.jpg", 4000, 3000);

    expect(result).toEqual({ uri: "file:///cache/resized.jpg", base64: "BASE64DATA", width: 1600, height: 1200 });
    expect(mockManipulateAsync).toHaveBeenCalledWith(
      "file:///cache/original.jpg",
      [{ resize: { width: 1600, height: 1200 } }],
      { compress: 0.8, format: "jpeg", base64: true },
    );
    expect(logger.warn).not.toHaveBeenCalled();
    expect(logger.debug).not.toHaveBeenCalled();
    // Never logs the source URI or the produced base64 string anywhere.
    const allLogCalls = [
      ...(logger.warn as jest.Mock).mock.calls,
      ...(logger.debug as jest.Mock).mock.calls,
    ].flat();
    expect(allLogCalls.join(" ")).not.toContain("BASE64DATA");
  });

  it("issues no resize action for a source already within the long-edge bound (never upscales)", async () => {
    mockManipulateAsync.mockResolvedValue({ uri: "file:///cache/resized.jpg", base64: "B64", width: 640, height: 480 });

    await prepareCaptureForSubmission("file:///cache/original.jpg", 640, 480);

    expect(mockManipulateAsync).toHaveBeenCalledWith(
      "file:///cache/original.jpg",
      [],
      { compress: 0.8, format: "jpeg", base64: true },
    );
  });

  it("throws rather than silently submitting no image data if base64 is missing, and best-effort-deletes the generated working file first", async () => {
    const deleteFn = jest.fn();
    MockFile.mockImplementation(() => ({ exists: true, delete: deleteFn }));
    mockManipulateAsync.mockResolvedValue({ uri: "file:///cache/resized.jpg", width: 1600, height: 1200 });

    await expect(prepareCaptureForSubmission("file:///cache/original.jpg", 4000, 3000)).rejects.toThrow();

    expect(MockFile).toHaveBeenCalledWith("file:///cache/resized.jpg");
    expect(deleteFn).toHaveBeenCalledTimes(1);
  });
});

describe("deleteLocalCaptureFile", () => {
  beforeEach(() => {
    MockFile.mockReset();
    (logger.warn as jest.Mock).mockReset();
  });

  it("deletes the file when it exists (handoff/retake/cancel all use this same primitive)", async () => {
    const deleteFn = jest.fn();
    MockFile.mockImplementation(() => ({ exists: true, delete: deleteFn }));

    await deleteLocalCaptureFile("file:///cache/original.jpg");

    expect(deleteFn).toHaveBeenCalledTimes(1);
  });

  it("is a no-op (not an error) when the file no longer exists", async () => {
    const deleteFn = jest.fn();
    MockFile.mockImplementation(() => ({ exists: false, delete: deleteFn }));

    await expect(deleteLocalCaptureFile("file:///cache/gone.jpg")).resolves.toBeUndefined();
    expect(deleteFn).not.toHaveBeenCalled();
  });

  it("never throws when deletion itself fails, and never logs the file URI", async () => {
    MockFile.mockImplementation(() => ({
      exists: true,
      delete: () => {
        throw new Error("permission denied");
      },
    }));

    await expect(deleteLocalCaptureFile("file:///cache/secret-face-photo.jpg")).resolves.toBeUndefined();
    expect(logger.warn).toHaveBeenCalledTimes(1);
    const loggedArgs = (logger.warn as jest.Mock).mock.calls[0];
    expect(loggedArgs.join(" ")).not.toContain("secret-face-photo");
  });
});
