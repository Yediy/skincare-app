import { Redirect, Stack } from "expo-router";
import React from "react";

import { useBootstrapRoute } from "@/hooks/use-bootstrap-route";
import { LoadingState } from "@/components/loading-state";

/** Defense in depth beyond app/index.tsx: reaching any (app) screen
 * while not actually past every onboarding gate (e.g. a deep link,
 * or consent being withdrawn mid-session) bounces back to the
 * central decision boundary instead of trusting that this screen was
 * reached legitimately. */
export default function AppLayout() {
  const { route } = useBootstrapRoute();
  if (route === "loading") {
    return <LoadingState />;
  }
  if (route !== "app") {
    return <Redirect href="/" />;
  }
  return <Stack screenOptions={{ headerShown: false }} />;
}
