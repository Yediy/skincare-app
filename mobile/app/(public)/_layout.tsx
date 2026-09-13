import { Redirect, Stack } from "expo-router";
import React from "react";

import { useSession } from "@/auth/session-context";

/** Defense in depth beyond app/index.tsx's own redirect: reaching any
 * (public) screen while already authenticated (e.g. a stale deep
 * link) bounces back to the central decision boundary rather than
 * showing sign-in over an active session. */
export default function PublicLayout() {
  const { state } = useSession();
  if (state.status === "AUTHENTICATED") {
    return <Redirect href="/" />;
  }
  return <Stack screenOptions={{ headerShown: false }} />;
}
