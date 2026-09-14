import React, { createContext, useCallback, useContext, useMemo, useState } from "react";

import { generateAnalysisRequestId } from "./request-id";

type AnalysisSessionValue = {
  /** Stable for the lifetime of one deliberate analysis attempt --
   * every retry of the same submission reuses this exact value. See
   * request-id.ts's own docstring for the full policy. */
  requestId: string;
  /** Starts a genuinely NEW analysis attempt: fresh request_id, and
   * clears any previously captured (but not yet submitted) image
   * reference. Call this only when the user deliberately chooses to
   * analyze again -- never on a retry of an in-flight/failed
   * submission. */
  startNewAttempt: () => void;
};

const AnalysisSessionContext = createContext<AnalysisSessionValue | null>(null);

/**
 * Wraps the analysis route group (app/(app)/analysis/_layout.tsx) so
 * every screen in the capture -> submit -> result flow shares the
 * same request_id without threading it through navigation params (an
 * analysis id only exists once submission succeeds; the request_id
 * exists before that). Holds no image bytes/URIs itself -- see
 * src/analysis/capture-file.ts for why that stays fully local to the
 * capture screen and is never lifted into shared state.
 */
export function AnalysisSessionProvider({ children }: { children: React.ReactNode }) {
  const [requestId, setRequestId] = useState(() => generateAnalysisRequestId());

  const startNewAttempt = useCallback(() => {
    setRequestId(generateAnalysisRequestId());
  }, []);

  const value = useMemo<AnalysisSessionValue>(() => ({ requestId, startNewAttempt }), [requestId, startNewAttempt]);

  return <AnalysisSessionContext.Provider value={value}>{children}</AnalysisSessionContext.Provider>;
}

export function useAnalysisSession(): AnalysisSessionValue {
  const ctx = useContext(AnalysisSessionContext);
  if (!ctx) {
    throw new Error("useAnalysisSession() must be used within an AnalysisSessionProvider");
  }
  return ctx;
}
