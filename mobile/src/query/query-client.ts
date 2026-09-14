import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/errors";

/**
 * One QueryClient for the whole app, created once near the root (see
 * app/_layout.tsx). Never persisted to disk (no persistQueryClient
 * wiring) -- authenticated data (profile, consent, allergies,
 * pregnancy/nursing) stays in-memory for the running session only,
 * per src/utils/storage-policy.ts. It IS explicitly cleared on
 * sign-out and account deletion -- see src/auth/session-context.tsx.
 */
export function createAppQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (failureCount, error) => {
          if (error instanceof ApiError && !error.retryable) return false;
          return failureCount < 2;
        },
        staleTime: 30_000,
      },
      mutations: {
        retry: false,
      },
    },
  });
}
