import {
  cameraReadinessReducer,
  canStartCapture,
  decideCaptureOwnership,
  INITIAL_CAMERA_READINESS_STATE,
} from "@/analysis/camera-capture-policy";

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

describe("cameraReadinessReducer (post-merge audit repair item 1: every newly mounted CameraView starts NOT READY)", () => {
  it("starts not ready", () => {
    expect(INITIAL_CAMERA_READINESS_STATE).toEqual({ cameraReady: false });
  });

  it("full capture -> review -> retake transition never lets a stale instance's readiness leak into the new one", () => {
    let state = INITIAL_CAMERA_READINESS_STATE;
    expect(canStartCapture({ cameraReady: state.cameraReady, capturePending: false })).toBe(false);

    // First CameraView instance reports ready.
    state = cameraReadinessReducer(state, { type: "CAMERA_READY" });
    expect(state.cameraReady).toBe(true);
    expect(canStartCapture({ cameraReady: state.cameraReady, capturePending: false })).toBe(true);

    // Capture succeeds -> transitioning to the review screen invalidates
    // the instance that produced it.
    state = cameraReadinessReducer(state, { type: "CAMERA_INVALIDATED" });
    expect(state.cameraReady).toBe(false);

    // Retake: a brand-new CameraView is about to mount. Readiness stays
    // false -- it must NOT inherit the previous instance's `true` --
    // until the new instance's own onCameraReady fires.
    state = cameraReadinessReducer(state, { type: "CAMERA_INVALIDATED" });
    expect(state.cameraReady).toBe(false);
    expect(canStartCapture({ cameraReady: state.cameraReady, capturePending: false })).toBe(false);

    // New instance reports ready.
    state = cameraReadinessReducer(state, { type: "CAMERA_READY" });
    expect(state.cameraReady).toBe(true);
    expect(canStartCapture({ cameraReady: state.cameraReady, capturePending: false })).toBe(true);
  });

  it("a mount error invalidates readiness", () => {
    const ready = cameraReadinessReducer(INITIAL_CAMERA_READINESS_STATE, { type: "CAMERA_READY" });
    const afterError = cameraReadinessReducer(ready, { type: "CAMERA_INVALIDATED" });
    expect(afterError.cameraReady).toBe(false);
  });
});

describe("decideCaptureOwnership (post-merge audit repair item 2: capture-in-flight unmount/orphan-file race)", () => {
  it("adopts the captured photo when the screen still owns the capture lifecycle", () => {
    expect(decideCaptureOwnership(true)).toBe("ADOPT");
  });

  it("discards the captured photo when the screen no longer owns the capture lifecycle (unmounted mid-flight)", () => {
    expect(decideCaptureOwnership(false)).toBe("DISCARD");
  });
});
