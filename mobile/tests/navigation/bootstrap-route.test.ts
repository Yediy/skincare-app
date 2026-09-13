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

describe("resolveBootstrapRoute", () => {
  it("never resolves to anything but loading while session is UNKNOWN, regardless of consent/profile", () => {
    expect(
      resolveBootstrapRoute({ session: { status: "UNKNOWN" }, consent: validConsent, profile: completeProfile }),
    ).toBe("loading");
    expect(resolveBootstrapRoute({ session: { status: "UNKNOWN" }, consent: undefined, profile: undefined })).toBe(
      "loading",
    );
  });

  it("routes signed-out sessions to public", () => {
    expect(
      resolveBootstrapRoute({ session: { status: "SIGNED_OUT" }, consent: undefined, profile: undefined }),
    ).toBe("public");
  });

  it("stays in loading while authenticated but gating data hasn't arrived yet", () => {
    expect(
      resolveBootstrapRoute({ session: { status: "AUTHENTICATED" }, consent: undefined, profile: completeProfile }),
    ).toBe("loading");
    expect(
      resolveBootstrapRoute({ session: { status: "AUTHENTICATED" }, consent: validConsent, profile: undefined }),
    ).toBe("loading");
  });

  it("routes to onboarding-consent when consent is missing or stale", () => {
    expect(
      resolveBootstrapRoute({ session: { status: "AUTHENTICATED" }, consent: missingConsent, profile: completeProfile }),
    ).toBe("onboarding-consent");
  });

  it("routes to onboarding-profile when consent is fine but profile was never set", () => {
    expect(
      resolveBootstrapRoute({ session: { status: "AUTHENTICATED" }, consent: validConsent, profile: incompleteProfile }),
    ).toBe("onboarding-profile");
  });

  it("consent is checked before profile -- both missing still lands on consent first", () => {
    expect(
      resolveBootstrapRoute({ session: { status: "AUTHENTICATED" }, consent: missingConsent, profile: incompleteProfile }),
    ).toBe("onboarding-consent");
  });

  it("routes to app once both gates are satisfied", () => {
    expect(
      resolveBootstrapRoute({ session: { status: "AUTHENTICATED" }, consent: validConsent, profile: completeProfile }),
    ).toBe("app");
  });
});
