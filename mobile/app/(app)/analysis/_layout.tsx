import { Stack } from "expo-router";
import React from "react";

/**
 * The (app) group's own layout already guarantees current
 * consent+profile before any (app)/* screen renders at all (section
 * 22/28). request_id identity is bound to each individual capture
 * attempt (src/analysis/capture-attempt.ts), not to this route
 * group's lifetime, so nothing here needs to be shared across screens
 * -- each screen reads/generates what it needs locally. If consent is
 * withdrawn or a new policy version is required mid-flow, that
 * surfaces as a 403 from POST /api/v2/analyses (never bypassed
 * client-side) and the capture screen routes back to onboarding-consent
 * from there -- see app/(app)/analysis/capture.tsx.
 */
export default function AnalysisLayout() {
  return <Stack screenOptions={{ headerShown: false }} />;
}
