import { Redirect, Stack } from "expo-router";
import React from "react";

import { ErrorState } from "@/components/error-state";
import { useBootstrapRoute } from "@/hooks/use-bootstrap-route";
import { LoadingState } from "@/components/loading-state";

/** Defense in depth beyond app/index.tsx: reaching any (app) screen
 * while not actually past every onboarding gate (e.g. a deep link,
 * or consent being withdrawn mid-session) bounces back to the
 * central decision boundary instead of trusting that this screen was
 * reached legitimately.
 *
 * "bootstrap-error" (mobile V1 repair pass): a gating query can fail
 * *after* a user is already inside the app shell (e.g. it was
 * `stale` and refetched on remount/focus) just as easily as at
 * initial launch -- this layout renders the same deliberate
 * reconnect surface rather than silently redirecting to "/" and
 * losing the user's place, or bouncing them into onboarding as if
 * their consent/profile were actually invalid. */
export default function AppLayout() {
  const { route, error, retry } = useBootstrapRoute();
  if (route === "loading") {
    return <LoadingState />;
  }
  if (route === "bootstrap-error") {
    return <ErrorState error={error} onRetry={retry} />;
  }
  if (route !== "app") {
    return <Redirect href="/" />;
  }
  return <Stack screenOptions={{ headerShown: false }} />;
}
