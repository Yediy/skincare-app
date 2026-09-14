import { useQuery } from "@tanstack/react-query";

import { getAnalysis } from "@/api/analysis-api";
import { queryKeys } from "@/query/keys";
import type { AnalysisStatusResponse } from "@/types/domain";

import { getPollingIntervalMs, mapBackendStatusToPhase, type MobileAnalysisPhase } from "./analysis-lifecycle";

export type UseAnalysisPollingResult = {
  data: AnalysisStatusResponse | undefined;
  phase: MobileAnalysisPhase;
  /** True exactly when the MOST RECENT poll attempt itself failed
   * (network/timeout/5xx/etc) -- never set merely because the analysis
   * itself is FAILED. TanStack Query keeps the last successfully
   * fetched `data` in place across a failed background refetch, so
   * `phase` above always reflects the last known-good backend status,
   * never flips to "failed" just because polling briefly couldn't
   * reach the server (section 13). */
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
 */
export function useAnalysisPolling(analysisId: string | null): UseAnalysisPollingResult {
  const query = useQuery<AnalysisStatusResponse>({
    queryKey: queryKeys.analysis(analysisId ?? "none"),
    queryFn: () => getAnalysis(analysisId as string),
    enabled: analysisId !== null,
    refetchInterval: (q) => {
      const status = q.state.data?.status;
      const phase = status ? mapBackendStatusToPhase(status) : "queued";
      return getPollingIntervalMs(phase, q.state.dataUpdateCount);
    },
    // A handful of automatic retries per attempt before this poll's
    // own failure surfaces as isPollingError -- refetchInterval above
    // is what keeps the OVERALL polling loop alive across many failed
    // attempts, this is just per-attempt resilience to one flaky
    // request.
    retry: (failureCount) => failureCount < 2,
    staleTime: 0,
  });

  const phase: MobileAnalysisPhase = query.data ? mapBackendStatusToPhase(query.data.status) : "queued";

  return {
    data: query.data,
    phase,
    isPollingError: query.isError,
    refetch: () => {
      query.refetch();
    },
  };
}
