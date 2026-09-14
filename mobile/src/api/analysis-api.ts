import { authorizedRequest } from "@/auth/auth-client-singleton";
import type { AnalysisStatusResponse, AnalysisSubmitResponse } from "@/types/domain";

export type SubmitAnalysisInput = {
  /** Base64-encoded JPEG. Never persisted, never logged, never placed
   * in the TanStack Query cache or global React state -- see
   * src/analysis/capture-file.ts, the one place this string is ever
   * produced. */
  image_base64: string;
  /** Generated once per deliberate analysis attempt
   * (src/analysis/request-id.ts) and reused across retries of that
   * same attempt -- see that module's own docstring. */
  request_id: string;
};

/** POST /api/v2/analyses -- the existing async analysis submission
 * endpoint (backend/app/api/v2/analyses.py). Goes through the same
 * authorizedRequest() boundary as every other authenticated call, so
 * a 401 here is transparently retried once by the single-flight
 * refresh coordinator before this promise ever rejects with one. */
export function submitAnalysis(input: SubmitAnalysisInput): Promise<AnalysisSubmitResponse> {
  return authorizedRequest<AnalysisSubmitResponse>("/api/v2/analyses", { method: "POST", body: input });
}

/** GET /api/v2/analyses/{id} -- current status, and (once COMPLETED)
 * the full result. Used both for the initial post-submit read and for
 * every subsequent poll (src/analysis/use-analysis-polling.ts). */
export function getAnalysis(analysisId: string): Promise<AnalysisStatusResponse> {
  return authorizedRequest<AnalysisStatusResponse>(`/api/v2/analyses/${analysisId}`);
}
