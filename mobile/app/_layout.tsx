import { QueryClientProvider } from "@tanstack/react-query";
import { Slot } from "expo-router";
import React, { useState } from "react";

import { SessionProvider } from "@/auth/session-context";
import { createAppQueryClient } from "@/query/query-client";
import { ThemeProvider } from "@/theme/theme-provider";

/**
 * Provider composition only -- the actual navigation decision lives
 * in app/index.tsx via useBootstrapRoute() (src/hooks/use-bootstrap-route.ts),
 * which is the one centralized boundary section 3 requires. No route
 * screen anywhere else calls router.replace()/router.push() based on
 * session/onboarding state on its own.
 */
export default function RootLayout() {
  const [queryClient] = useState(() => createAppQueryClient());

  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <Slot />
        </SessionProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
