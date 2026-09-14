import { randomUUID } from "expo-crypto";

/**
 * request_id identity policy for async analysis submission (section 9
 * of this pass's brief), matching the backend's own idempotency
 * contract exactly (backend/app/domain/analysis_submission_service.py,
 * backend/app/db/usage_repository.py):
 *
 *   - A NEW deliberate analysis attempt (the user starts fresh from
 *     the Capture Coach, or explicitly asks to "Analyze again" after a
 *     completed/failed result) gets a NEW request_id.
 *   - Retrying the SAME submission -- a network error, a retake before
 *     the first successful submit, re-pressing "Submit" after a
 *     transient failure -- reuses the SAME request_id.
 *
 * This is deliberately a plain function, not a hook, so "generate a
 * fresh id" and "the current id" are two unambiguous, separately
 * callable operations -- src/analysis/analysis-session.tsx is the one
 * place that decides *when* each happens (holding exactly one id in
 * React state for the lifetime of one analysis attempt), never this
 * module itself.
 */
export function generateAnalysisRequestId(): string {
  return randomUUID();
}
