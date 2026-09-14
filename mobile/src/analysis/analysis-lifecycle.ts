import type { AnalysisRequestStatus } from "@/types/domain";

/**
 * Mobile's own domain representation of an analysis attempt (section
 * 11). `draft`/`submitting` are client-only phases that exist before
 * the backend has even durably created an analysis_requests row;
 * every other phase maps 1:1 onto backend truth
 * (backend/app/db/analysis_repository.py's status column) -- mobile
 * never invents a backend lifecycle state that doesn't exist.
 */
export type MobileAnalysisPhase = "draft" | "submitting" | "queued" | "processing" | "completed" | "failed";

/**
 * RECEIVED folds into "queued" (both mean "not yet claimed by a
 * worker" from this client's point of view -- there is no UI
 * distinction worth making between them) and CANCELLED folds into
 * "failed" (the honest closest client-facing phase; this app has no
 * cancellation UI of its own in Phase B, but a request could still
 * reach this state some other way and must render as SOMETHING real,
 * never silently as still-in-progress).
 */
export function mapBackendStatusToPhase(status: AnalysisRequestStatus): MobileAnalysisPhase {
  switch (status) {
    case "RECEIVED":
    case "QUEUED":
      return "queued";
    case "PROCESSING":
      return "processing";
    case "COMPLETED":
      return "completed";
    case "FAILED":
    case "CANCELLED":
      return "failed";
    default:
      return "failed";
  }
}

export function isTerminalPhase(phase: MobileAnalysisPhase): boolean {
  return phase === "completed" || phase === "failed";
}

// Section 12: "1.5-3 seconds initially... a modest adaptive backoff is
// acceptable... do not poll every 100ms." Plain numbers, not
// calibrated against any measured backend SLA -- a deliberately simple
// policy documented here, not hidden inside the hook that uses it.
export const INITIAL_POLL_INTERVAL_MS = 2000;
export const BACKOFF_POLL_INTERVAL_MS = 4000;
/** After this many successful polls still not terminal, back off from
 * the initial interval -- most real analyses complete well before
 * this (seconds, per WORKER_OPERATIONS.md), so this only ever matters
 * for a genuinely slow/stuck job. */
export const BACKOFF_AFTER_POLL_COUNT = 5;

/**
 * Pure polling-interval decision (section 12): the hook's own
 * `refetchInterval` callback delegates here so this policy is
 * testable without rendering anything or faking timers. `false` means
 * "stop polling" -- TanStack Query treats that as "do not schedule
 * another refetch."
 */
export function getPollingIntervalMs(phase: MobileAnalysisPhase, successfulPollCount: number): number | false {
  if (isTerminalPhase(phase)) return false;
  return successfulPollCount >= BACKOFF_AFTER_POLL_COUNT ? BACKOFF_POLL_INTERVAL_MS : INITIAL_POLL_INTERVAL_MS;
}
