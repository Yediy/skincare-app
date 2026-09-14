import { Redirect } from "expo-router";
import React from "react";

import { ErrorState } from "@/components/error-state";
import { LoadingState } from "@/components/loading-state";
import { useBootstrapRoute } from "@/hooks/use-bootstrap-route";

/**
 * The one route that must always exist and always resolve (section
 * 3). Every session/onboarding/app decision funnels through
 * useBootstrapRoute() -- this file contains no logic of its own
 * beyond mapping its output to a target path.
 *
 * "bootstrap-error" (mobile V1 repair pass): an authenticated user
 * whose consent/profile fetch failed (offline at launch, timeout, a
 * backend 5xx) gets a deliberate reconnect surface with a Retry
 * button here, never an infinite spinner -- see
 * src/navigation/bootstrap-route.ts's own docstring for why this
 * state exists.
 */
export default function Index() {
  const { route, error, retry } = useBootstrapRoute();

  switch (route) {
    case "loading":
      return <LoadingState label="Preparing your session…" />;
    case "bootstrap-error":
      return <ErrorState error={error} onRetry={retry} />;
    case "public":
      return <Redirect href="/(public)/welcome" />;
    case "onboarding-consent":
      return <Redirect href="/(onboarding)/consent" />;
    case "onboarding-profile":
      return <Redirect href="/(onboarding)/profile" />;
    case "app":
      return <Redirect href="/(app)" />;
  }
}
