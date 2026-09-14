import { Stack } from "expo-router";
import React from "react";

import { AnalysisSessionProvider } from "@/analysis/analysis-session";

/**
 * The (app) group's own layout already guarantees current
 * consent+profile before any (app)/* screen renders at all (section
 * 22/28) -- this layout adds nothing on top of that beyond wrapping
 * the capture -> submit -> result flow in one AnalysisSessionProvider,
 * so every screen in this group shares the same request_id. If
 * consent is withdrawn or a new policy version is required mid-flow,
 * that surfaces as a 403 from POST /api/v2/analyses (never bypassed
 * client-side) and the capture screen routes back to onboarding-consent
 * from there -- see app/(app)/analysis/capture.tsx.
 */
export default function AnalysisLayout() {
  return (
    <AnalysisSessionProvider>
      <Stack screenOptions={{ headerShown: false }} />
    </AnalysisSessionProvider>
  );
}
