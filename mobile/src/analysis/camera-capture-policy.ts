/**
 * Pure capture-readiness/concurrency policy (section 4 of the Mobile
 * Phase B post-merge audit repair pass). `expo-camera`'s own docs
 * require waiting for `onCameraReady` before calling
 * `takePictureAsync()` on a real device; nothing in the previous
 * implementation enforced that, and nothing prevented a rapid double
 * tap of the Capture button from firing two concurrent
 * `takePictureAsync()` calls (which can orphan a temp file or produce
 * two photos for one user action). This module holds the decision
 * logic only -- app/(app)/analysis/capture.tsx owns the actual
 * `cameraReady`/`capturePending`/`captureError` React state and calls
 * `canStartCapture()` before ever invoking `takePictureAsync()`.
 */
export type CaptureReadinessState = {
  cameraReady: boolean;
  capturePending: boolean;
};

/** True only when the camera has reported ready AND no capture is
 * already in flight -- both must hold for a new `takePictureAsync()`
 * call to be safe. */
export function canStartCapture(state: CaptureReadinessState): boolean {
  return state.cameraReady && !state.capturePending;
}

/** Safe, nontechnical copy shown to the user -- never a raw
 * exception message, and never anything that could embed a file URI. */
export const CAPTURE_FAILURE_MESSAGE = "We couldn't take that photo. Please try again.";
export const CAMERA_INIT_FAILURE_MESSAGE =
  "We couldn't start the camera. Please check camera access or try again.";
