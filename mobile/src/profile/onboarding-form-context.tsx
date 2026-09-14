import React, { createContext, useContext, useState } from "react";

import type { ProfileUpdateRequest } from "@/api/types";

const DEFAULTS: ProfileUpdateRequest = {
  has_sensitive_skin: false,
  experience_level: "beginner",
  max_routine_steps: 10,
  is_pregnant: false,
  is_nursing: false,
  allergies: [],
  avoid_ingredients: [],
  skin_goals: [],
};

type OnboardingFormContextValue = {
  form: ProfileUpdateRequest;
  update: (patch: Partial<ProfileUpdateRequest>) => void;
};

const OnboardingFormContext = createContext<OnboardingFormContextValue | null>(null);

/**
 * Backend truth is one PUT /profile call carrying the entire profile
 * -- there is no partial-patch endpoint. The onboarding wizard
 * (consent -> profile -> goals -> constraints) collects fields across
 * three screens but submits exactly once, on the last screen, so a
 * later step never has a chance to overwrite an earlier step's answer
 * with a stale default.
 */
export function OnboardingFormProvider({ children }: { children: React.ReactNode }) {
  const [form, setForm] = useState<ProfileUpdateRequest>(DEFAULTS);
  const update = (patch: Partial<ProfileUpdateRequest>) => setForm((prev) => ({ ...prev, ...patch }));
  return <OnboardingFormContext.Provider value={{ form, update }}>{children}</OnboardingFormContext.Provider>;
}

export function useOnboardingForm(): OnboardingFormContextValue {
  const ctx = useContext(OnboardingFormContext);
  if (!ctx) {
    throw new Error("useOnboardingForm() must be used within an OnboardingFormProvider");
  }
  return ctx;
}
