import { randomUUID } from "expo-crypto";

/**
 * request_id identity policy for async analysis submission, matching
 * the backend's own idempotency contract exactly
 * (backend/app/domain/analysis_submission_service.py,
 * backend/app/db/usage_repository.py):
 *
 *   - A NEW captured photo (a fresh capture, or a Retake replacing a
 *     discarded one) gets a NEW request_id.
 *   - Retrying the submission of the SAME captured photo -- a network
 *     error, re-pressing "Use this photo" after a transient failure --
 *     reuses the SAME request_id.
 *
 * This is deliberately a plain function, not a hook: identity is
 * bound to the capture attempt itself, not to how long a screen or
 * navigation session has been alive. See
 * src/analysis/capture-attempt.ts (the pure reducer that decides
 * *when* a new id is minted) and its own docstring for the full
 * rationale and the defect this replaced.
 */
export function generateAnalysisRequestId(): string {
  return randomUUID();
}
