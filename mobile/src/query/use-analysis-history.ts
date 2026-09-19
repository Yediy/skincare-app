import { useInfiniteQuery } from "@tanstack/react-query";

import { getAnalysisHistory } from "@/api/analysis-api";

import { queryKeys } from "./keys";

/**
 * Mobile C3 (history): server-paginated list of the calling user's
 * own past analyses, newest first. Server is the sole source of
 * ownership truth (GET /api/v2/analyses is RLS-scoped) -- this hook
 * never merges in any locally-held analysis state.
 */
export function useAnalysisHistoryQuery(options: { enabled: boolean }) {
  return useInfiniteQuery({
    queryKey: queryKeys.analysisHistory,
    queryFn: ({ pageParam }) => getAnalysisHistory({ cursor: pageParam }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: options.enabled,
  });
}
