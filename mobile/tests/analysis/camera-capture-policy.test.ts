import { canStartCapture } from "@/analysis/camera-capture-policy";

describe("canStartCapture", () => {
  it("is false before the camera reports ready", () => {
    expect(canStartCapture({ cameraReady: false, capturePending: false })).toBe(false);
  });

  it("is false while a capture is already in flight, even if the camera is ready", () => {
    expect(canStartCapture({ cameraReady: true, capturePending: true })).toBe(false);
  });

  it("is false when neither condition holds", () => {
    expect(canStartCapture({ cameraReady: false, capturePending: true })).toBe(false);
  });

  it("is true only once ready and not already capturing -- prevents two concurrent takePictureAsync() calls from a rapid double tap", () => {
    expect(canStartCapture({ cameraReady: true, capturePending: false })).toBe(true);
  });
});
