/** Mirrors backend/app/main.py response shapes exactly -- this app
 * never invents fields the backend doesn't actually return. */

export type ExperienceLevel = "beginner" | "intermediate" | "advanced";

export type Profile = {
  has_sensitive_skin: boolean;
  experience_level: ExperienceLevel;
  max_routine_steps: number;
  is_pregnant: boolean;
  is_nursing: boolean;
  allergies: string[];
  avoid_ingredients: string[];
  skin_goals: string[];
  /** True only once the user has ever explicitly saved a profile --
   * distinguishes "never onboarded" from "chose every default." See
   * backend/app/db/profile_repository.py. */
  profile_set: boolean;
};

export type ProfileUpdateInput = Omit<Profile, "profile_set">;

export type ConsentStatus = {
  consent_type: string;
  required_policy_version: string;
  has_valid_consent: boolean;
};

export type AuthTokens = {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
};

export type MeResponse = {
  user_id: string;
};
