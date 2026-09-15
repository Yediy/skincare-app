import { useQuery } from "@tanstack/react-query";

import { getAnalysis } from "@/api/analysis-api";
import { ApiError } from "@/api/errors";
import { queryKeys } from "@/query/keys";
import type { AnalysisStatusResponse } from "@/types/domain";

import {
  derivePollingOutcome,
  getPollingIntervalMs,
  isPermanentPollingError,
  mapBackendStatusToPhase,
  type MobileAnalysisPhase,
} from "./analysis-lifecycle";

export type UseAnalysisPollingResult = {
  data: AnalysisStatusResponse | undefined;
  phase: MobileAnalysisPhase;
  /** True exactly when the MOST RECENT poll attempt failed with a
   * TRANSIENT error (network/timeout/5xx/retryable-429) -- never set
   * merely because the analysis itself is FAILED, and never set for a
   * PERMANENT error (that surfaces as phase === "unavailable"
   * instead, see below). TanStack Query keeps the last successfully
   * fetched `data` in place across a failed background refetch, so
   * `phase` above always reflects the last known-good backend status
   * while this is true, never flips to "failed" just because polling
   * briefly couldn't reach the server (section 13). */
  isPollingError: boolean;
  refetch: () => void;
};

/**
 * Section 12: TanStack Query drives polling, not a hand-rolled
 * `setInterval` in a route component. `refetchInterval` delegates the
 * actual interval-vs-stop decision to the pure
 * getPollingIntervalMs()/mapBackendStatusToPhase() functions
 * (analysis-lifecycle.ts) so that policy is unit-tested without
 * rendering anything. `enabled: analysisId !== null` plus unmounting
 * the component calling this hook is what actually stops network
 * activity -- TanStack Query cancels its own interval timer on
 * unmount automatically.
 *
 * Section 8 of the Mobile Phase B post-merge audit repair pass: a
 * PERMANENT failure (404/403/other nonretryable 4xx,
 * `ApiError.retryable === false`) must stop polling outright and
 * render as a deliberate "unavailable" phase, never as indefinitely
 * "queued" and never silently retried forever the way the previous
 * implementation did (it read `q.state.data?.status`, which stays
 * `undefined` forever on a request that has never once succeeded, and
 * `undefined` mapped to the "queued" fallback with no error check at
 * all). A TRANSIENT failure (network/timeout/5xx/retryable-429) must
 * do none of that -- it preserves whatever phase was last known-good
 * and keeps retrying, handled by leaving the existing behavior below
 * completely unchanged for that case.
 */
export function useAnalysisPolling(analysisId: string | null): UseAnalysisPollingResult {
  const query = useQuery<AnalysisStatusResponse>({
    queryKey: queryKeys.analysis(analysisId ?? "none"),
    queryFn: () => getAnalysis(analysisId as string),
    enabled: analysisId !== null,
    refetchInterval: (q) => {
      if (isPermanentPollingError(q.state.error)) return false;
      const status = q.state.data?.status;
      const phase = status ? mapBackendStatusToPhase(status) : "queued";
      return getPollingIntervalMs(phase, q.state.dataUpdateCount);
    },
    // A handful of automatic retries per attempt before this poll's
    // own failure surfaces as isPollingError -- refetchInterval above
    // is what keeps the OVERALL polling loop alive across many failed
    // TRANSIENT attempts, this is just per-attempt resilience to one
    // flaky request. A PERMANENT error (retryable: false) gets zero
    // retries: there is no value in re-attempting a request the
    // server has already told us definitively cannot succeed.
    retry: (failureCount, error) => {
      if (error instanceof ApiError && !error.retryable) return false;
      return failureCount < 2;
    },
    staleTime: 0,
  });

  const { phase, isPollingError } = derivePollingOutcome(query.data, query.error, query.isError);

  return {
    data: query.data,
    phase,
    isPollingError,
    refetch: () => {
      query.refetch();
    },
  };
}
