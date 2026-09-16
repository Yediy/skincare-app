import { generateAnalysisRequestId } from "./request-id";

/**
 * A single captured-but-not-yet-confirmed photo, and the ONE request_id
 * that identifies submitting it (section 1 of the Mobile Phase B
 * post-merge audit repair pass).
 *
 * This replaces the previous "one request_id per route-session"
 * design (analysis-session.tsx, now removed): that let a failed
 * submission followed by Retake resubmit a *different* photo under
 * the *same* request_id, which the backend's idempotency contract
 * would then honor literally -- returning the first photo's analysis
 * for the second photo's submission. Binding identity to the capture
 * attempt itself instead of the screen's lifetime makes that
 * impossible: a request_id only ever exists alongside the exact photo
 * it was minted for, and a new photo cannot be captured without also
 * minting a new one.
 *
 * Invariant (see tests/analysis/capture-attempt.test.ts):
 *   - CAPTURED always mints a fresh, cryptographically random
 *     request_id -- there is no code path that captures a new photo
 *     and reuses an old id.
 *   - DISCARDED (Retake, Cancel-after-capture) clears the attempt
 *     entirely; the discarded photo's request_id is never seen again.
 *   - Re-submitting the SAME CaptureAttempt object (a network retry, a
 *     re-tap of "Use this photo" after a transient failure) is simply
 *     not represented here at all -- it is the *absence* of a new
 *     action, so the existing attempt's request_id is trivially
 *     reused by construction, never regenerated.
 */
export type CaptureAttempt = {
  /** Local file URI of the original, unmodified camera capture. */
  uri: string;
  /** Pixel dimensions as reported by the camera capture itself (never
   * guessed from the URI) -- see src/analysis/capture-resize.ts. */
  width: number;
  height: number;
  /** Minted once, exactly when this photo was captured; stable for
   * the lifetime of this CaptureAttempt object. */
  requestId: string;
};

export type CaptureAttemptAction =
  | { type: "CAPTURED"; uri: string; width: number; height: number }
  | { type: "DISCARDED" };

/**
 * Pure reducer -- no React dependency, so the request-id invariant
 * above is unit-testable without rendering anything.
 * `generateRequestId` is injectable purely for deterministic tests;
 * every real caller uses the default.
 */
export function captureAttemptReducer(
  state: CaptureAttempt | null,
  action: CaptureAttemptAction,
  generateRequestId: () => string = generateAnalysisRequestId,
): CaptureAttempt | null {
  switch (action.type) {
    case "CAPTURED":
      return {
        uri: action.uri,
        width: action.width,
        height: action.height,
        requestId: generateRequestId(),
      };
    case "DISCARDED":
      return null;
    default:
      return state;
  }
}
