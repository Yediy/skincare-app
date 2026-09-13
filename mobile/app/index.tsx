import { Redirect } from "expo-router";
import React from "react";

import { LoadingState } from "@/components/loading-state";
import { useBootstrapRoute } from "@/hooks/use-bootstrap-route";

/**
 * The one route that must always exist and always resolve (section
 * 3). Every session/onboarding/app decision funnels through
 * useBootstrapRoute() -- this file contains no logic of its own
 * beyond mapping its output to a target path.
 */
export default function Index() {
  const { route } = useBootstrapRoute();

  switch (route) {
    case "loading":
      return <LoadingState label="Preparing your session…" />;
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
