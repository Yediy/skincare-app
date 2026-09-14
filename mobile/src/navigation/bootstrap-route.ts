import type { ConsentStatus, Profile } from "@/types/domain";
import type { SessionState } from "@/auth/restore-session";

export type BootstrapRoute =
  | "loading"
  | "public"
  | "bootstrap-error"
  | "onboarding-consent"
  | "onboarding-profile"
  | "app";

/**
 * The gating queries' state, reduced to exactly what a routing
 * decision needs -- not the full TanStack Query result object, so
 * this stays trivially constructible in a unit test without a real
 * QueryClient.
 */
export type GatingQueryState<T> = {
  data: T | undefined;
  /** True once retries are exhausted and the query has settled into a
   * failed state -- NOT true merely because a request is in flight or
   * because an earlier attempt failed and a retry is pending (that's
   * still `data === undefined` with `isError === false`, correctly
   * "loading"). See src/query/query-client.ts's retry policy. */
  isError: boolean;
};

export type BootstrapInput = {
  session: SessionState;
  consent: GatingQueryState<ConsentStatus>;
  profile: GatingQueryState<Profile>;
};

/**
 * THE single centralized navigation decision boundary (section 3):
 * every redirect in this app is a consequence of this function's
 * output, computed once in app/_layout.tsx, never scattered as
 * ad-hoc `router.replace()` calls inside individual screens.
 *
 * Pure and synchronous on purpose -- trivially unit-testable without
 * rendering anything, and there is exactly one place to audit for
 * "under what conditions does a user see the app shell."
 *
 * `bootstrap-error` (mobile V1 repair pass): before this state
 * existed, an authenticated user whose consent/profile fetch settled
 * into a permanent failure (offline at launch, a timeout, a backend
 * 5xx -- anything that isn't a 401/403, which the refresh coordinator
 * already handles by ending the session) had `data` stuck at
 * `undefined` forever, which this function's own "still loading"
 * branch below would read as `"loading"` indefinitely -- an infinite
 * spinner with no way out. The error check below runs BEFORE the
 * still-loading check specifically because a settled error and "still
 * fetching" both leave `data === undefined`; checking loading first
 * would swallow the error case right back into the same infinite
 * spinner this state exists to eliminate. Never signs the user out or
 * touches stored credentials -- that stays exclusively the refresh
 * coordinator's job for a definite 401/403, never a query-layer
 * concern (see src/auth/refresh-coordinator.ts).
 */
export function resolveBootstrapRoute(input: BootstrapInput): BootstrapRoute {
  if (input.session.status === "UNKNOWN") {
    return "loading";
  }
  if (input.session.status === "SIGNED_OUT") {
    return "public";
  }

  // AUTHENTICATED from here on.
  if (input.consent.isError || input.profile.isError) {
    return "bootstrap-error";
  }

  // Gating data itself is still in flight -- this is still a loading
  // state, not a decision to show the public flow or the app (section
  // 4: never flash the wrong interface because a fetch hasn't
  // resolved yet).
  if (input.consent.data === undefined || input.profile.data === undefined) {
    return "loading";
  }

  if (!input.consent.data.has_valid_consent) {
    return "onboarding-consent";
  }
  if (!input.profile.data.profile_set) {
    return "onboarding-profile";
  }
  return "app";
}
