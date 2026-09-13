import type { ConsentStatus, Profile } from "@/types/domain";
import type { SessionState } from "@/auth/restore-session";

export type BootstrapRoute =
  | "loading"
  | "public"
  | "onboarding-consent"
  | "onboarding-profile"
  | "app";

export type BootstrapInput = {
  session: SessionState;
  /** undefined = still loading; only meaningful once session is
   * AUTHENTICATED. */
  consent: ConsentStatus | undefined;
  profile: Profile | undefined;
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
 */
export function resolveBootstrapRoute(input: BootstrapInput): BootstrapRoute {
  if (input.session.status === "UNKNOWN") {
    return "loading";
  }
  if (input.session.status === "SIGNED_OUT") {
    return "public";
  }

  // AUTHENTICATED, but the gating data itself is still in flight --
  // this is still a loading state, not a decision to show the public
  // flow or the app (section 4: never flash the wrong interface
  // because a fetch hasn't resolved yet).
  if (input.consent === undefined || input.profile === undefined) {
    return "loading";
  }

  if (!input.consent.has_valid_consent) {
    return "onboarding-consent";
  }
  if (!input.profile.profile_set) {
    return "onboarding-profile";
  }
  return "app";
}
