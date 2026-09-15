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

/**
 * Pure camera-readiness state machine (post-merge audit repair pass,
 * item 1). `cameraReady` was previously a plain `useState<boolean>` in
 * app/(app)/analysis/capture.tsx that persisted across a successful
 * capture: the live `CameraView` unmounts once the review screen
 * shows, but `cameraReady` stayed `true`. On Retake a brand-new
 * `CameraView` mounts (a fresh native camera instance, which has never
 * fired `onCameraReady`) while stale `cameraReady=true` from the
 * PREVIOUS instance let the Capture button become active before the
 * new instance was actually ready.
 *
 * Required invariant: EVERY newly mounted `CameraView` starts NOT
 * READY. This reducer enforces it structurally rather than by
 * convention -- the only event that can ever produce `cameraReady:
 * true` is `CAMERA_READY`, fired by the currently-mounted instance's
 * own `onCameraReady` callback, and `CAMERA_INVALIDATED` is dispatched
 * on every path that leaves the live camera (a successful capture) or
 * returns to it (Retake/Cancel-after-capture), so `cameraReady` is
 * always `false` for the entire span between "we stopped trusting the
 * old instance" and "the new instance told us it's ready."
 */
export type CameraReadinessState = {
  cameraReady: boolean;
};

export const INITIAL_CAMERA_READINESS_STATE: CameraReadinessState = { cameraReady: false };

export type CameraReadinessEvent =
  | { type: "CAMERA_READY" }
  | { type: "CAMERA_INVALIDATED" };

export function cameraReadinessReducer(
  state: CameraReadinessState,
  event: CameraReadinessEvent,
): CameraReadinessState {
  switch (event.type) {
    case "CAMERA_READY":
      return { cameraReady: true };
    case "CAMERA_INVALIDATED":
      return { cameraReady: false };
    default:
      return state;
  }
}

/**
 * Pure capture-ownership decision (post-merge audit repair pass, item
 * 2). `takePictureAsync()` is an in-flight native promise that can
 * outlive the screen that started it -- system Back, a gesture, a
 * deep link, or any other navigation can unmount
 * app/(app)/analysis/capture.tsx while the promise is still pending.
 * If that promise resolves after the screen has lost ownership of the
 * capture lifecycle, the returned photo must never be adopted as a
 * `CaptureAttempt` (dispatching CAPTURED into a reducer nobody will
 * ever read again, and updating React state on a component that's
 * gone) -- it must instead be deleted so it doesn't sit in the OS
 * cache directory as an orphaned facial-image file. The caller tracks
 * ownership itself (an `isMountedRef`-style ref is the standard React
 * pattern for this); this function is the pure decision only, kept
 * separate so the policy is unit-testable without rendering a
 * `CameraView`.
 */
export type CaptureOwnershipDecision = "ADOPT" | "DISCARD";

export function decideCaptureOwnership(stillOwnsCaptureLifecycle: boolean): CaptureOwnershipDecision {
  return stillOwnsCaptureLifecycle ? "ADOPT" : "DISCARD";
}
