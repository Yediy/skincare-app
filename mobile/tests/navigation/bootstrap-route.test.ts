import { resolveBootstrapRoute } from "@/navigation/bootstrap-route";
import type { ConsentStatus, Profile } from "@/types/domain";

const validConsent: ConsentStatus = {
  consent_type: "facial_analysis",
  required_policy_version: "1.0",
  has_valid_consent: true,
};

const missingConsent: ConsentStatus = { ...validConsent, has_valid_consent: false };

const completeProfile: Profile = {
  has_sensitive_skin: false,
  experience_level: "beginner",
  max_routine_steps: 10,
  is_pregnant: false,
  is_nursing: false,
  allergies: [],
  avoid_ingredients: [],
  skin_goals: [],
  profile_set: true,
};

const incompleteProfile: Profile = { ...completeProfile, profile_set: false };

const notLoaded = { data: undefined, isError: false };
const errored = { data: undefined, isError: true };
const loadedConsent = (consent: ConsentStatus) => ({ data: consent, isError: false });
const loadedProfile = (profile: Profile) => ({ data: profile, isError: false });

describe("resolveBootstrapRoute", () => {
  it("never resolves to anything but loading while session is UNKNOWN, regardless of consent/profile", () => {
    expect(
      resolveBootstrapRoute({
        session: { status: "UNKNOWN" },
        consent: loadedConsent(validConsent),
        profile: loadedProfile(completeProfile),
      }),
    ).toBe("loading");
    expect(
      resolveBootstrapRoute({ session: { status: "UNKNOWN" }, consent: notLoaded, profile: notLoaded }),
    ).toBe("loading");
  });

  it("routes signed-out sessions to public", () => {
    expect(
      resolveBootstrapRoute({ session: { status: "SIGNED_OUT" }, consent: notLoaded, profile: notLoaded }),
    ).toBe("public");
  });

  it("stays in loading while authenticated but gating data hasn't arrived yet", () => {
    expect(
      resolveBootstrapRoute({
        session: { status: "AUTHENTICATED" },
        consent: notLoaded,
        profile: loadedProfile(completeProfile),
      }),
    ).toBe("loading");
    expect(
      resolveBootstrapRoute({
        session: { status: "AUTHENTICATED" },
        consent: loadedConsent(validConsent),
        profile: notLoaded,
      }),
    ).toBe("loading");
  });

  it("routes to onboarding-consent when consent is missing or stale", () => {
    expect(
      resolveBootstrapRoute({
        session: { status: "AUTHENTICATED" },
        consent: loadedConsent(missingConsent),
        profile: loadedProfile(completeProfile),
      }),
    ).toBe("onboarding-consent");
  });

  it("routes to onboarding-profile when consent is fine but profile was never set", () => {
    expect(
      resolveBootstrapRoute({
        session: { status: "AUTHENTICATED" },
        consent: loadedConsent(validConsent),
        profile: loadedProfile(incompleteProfile),
      }),
    ).toBe("onboarding-profile");
  });

  it("consent is checked before profile -- both missing still lands on consent first", () => {
    expect(
      resolveBootstrapRoute({
        session: { status: "AUTHENTICATED" },
        consent: loadedConsent(missingConsent),
        profile: loadedProfile(incompleteProfile),
      }),
    ).toBe("onboarding-consent");
  });

  it("routes to app once both gates are satisfied", () => {
    expect(
      resolveBootstrapRoute({
        session: { status: "AUTHENTICATED" },
        consent: loadedConsent(validConsent),
        profile: loadedProfile(completeProfile),
      }),
    ).toBe("app");
  });

  // --- bootstrap-error (mobile V1 repair pass) --------------------

  it("authenticated + consent settled into an error (e.g. NETWORK_ERROR) -> bootstrap-error, never stuck loading", () => {
    expect(
      resolveBootstrapRoute({
        session: { status: "AUTHENTICATED" },
        consent: errored,
        profile: loadedProfile(completeProfile),
      }),
    ).toBe("bootstrap-error");
  });

  it("authenticated + profile settled into an error (e.g. TIMEOUT) -> bootstrap-error", () => {
    expect(
      resolveBootstrapRoute({
        session: { status: "AUTHENTICATED" },
        consent: loadedConsent(validConsent),
        profile: errored,
      }),
    ).toBe("bootstrap-error");
  });

  it("authenticated + backend 5xx on either gating query -> bootstrap-error", () => {
    // classifyStatus(5xx) => SERVER_ERROR upstream; resolveBootstrapRoute
    // doesn't need the specific code, only that the query settled failed.
    expect(
      resolveBootstrapRoute({ session: { status: "AUTHENTICATED" }, consent: errored, profile: notLoaded }),
    ).toBe("bootstrap-error");
  });

  it("one gating query succeeds and the other fails -> bootstrap-error, not app/onboarding", () => {
    expect(
      resolveBootstrapRoute({
        session: { status: "AUTHENTICATED" },
        consent: loadedConsent(validConsent),
        profile: errored,
      }),
    ).toBe("bootstrap-error");
    expect(
      resolveBootstrapRoute({
        session: { status: "AUTHENTICATED" },
        consent: errored,
        profile: loadedProfile(completeProfile),
      }),
    ).toBe("bootstrap-error");
  });

  it("a successful retry (isError flips back to false, data arrives) advances past bootstrap-error to the correct route", () => {
    const afterRetry = resolveBootstrapRoute({
      session: { status: "AUTHENTICATED" },
      consent: loadedConsent(validConsent),
      profile: loadedProfile(completeProfile),
    });
    expect(afterRetry).toBe("app");
  });

  it("bootstrap-error never depends on session status flipping -- session stays AUTHENTICATED throughout", () => {
    const input = {
      session: { status: "AUTHENTICATED" as const },
      consent: errored,
      profile: notLoaded,
    };
    expect(resolveBootstrapRoute(input)).toBe("bootstrap-error");
    // Session itself is untouched by this function -- it only reads
    // input.session, never mutates or infers a different status from
    // the error. Asserting the input's own session field is still
    // AUTHENTICATED documents that this function has no side channel
    // to sign anyone out.
    expect(input.session.status).toBe("AUTHENTICATED");
  });
});
