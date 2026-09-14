import { ApiError } from "@/api/errors";
import { getProfile, updateProfile } from "@/api/profile-api";
import { authorizedRequest } from "@/auth/auth-client-singleton";

// Never the real SecureStore-backed singleton (see
// auth-client-singleton.ts's own docstring on why that's deliberately
// untested directly) -- jest auto-mocks every export as a jest.fn()
// (hoisted above these imports at compile time regardless of source
// position), which this file then configures per test.
jest.mock("@/auth/auth-client-singleton");

const mockAuthorizedRequest = authorizedRequest as jest.Mock;

describe("profile-api", () => {
  beforeEach(() => {
    mockAuthorizedRequest.mockReset();
  });

  it("getProfile() reads GET /profile through the authorized request boundary", async () => {
    const profile = {
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
    mockAuthorizedRequest.mockResolvedValue(profile);

    await expect(getProfile()).resolves.toEqual(profile);
    expect(mockAuthorizedRequest).toHaveBeenCalledWith("/profile");
  });

  it("updateProfile() sends a PUT with the full profile body", async () => {
    mockAuthorizedRequest.mockResolvedValue({ detail: "Profile updated" });
    const input = {
      has_sensitive_skin: true,
      experience_level: "advanced" as const,
      max_routine_steps: 6,
      is_pregnant: false,
      is_nursing: false,
      allergies: ["fragrance"],
      avoid_ingredients: [],
      skin_goals: ["EVENNESS_TONE"],
    };

    await updateProfile(input);

    expect(mockAuthorizedRequest).toHaveBeenCalledWith("/profile", { method: "PUT", body: input });
  });

  it("propagates a backend validation error untouched for the screen to render", async () => {
    const error = new ApiError({ status: 422, code: "VALIDATION_ERROR", message: "Invalid experience level", retryable: false });
    mockAuthorizedRequest.mockRejectedValue(error);

    await expect(
      updateProfile({
        has_sensitive_skin: false,
        experience_level: "beginner",
        max_routine_steps: 10,
        is_pregnant: false,
        is_nursing: false,
        allergies: [],
        avoid_ingredients: [],
        skin_goals: [],
      }),
    ).rejects.toBe(error);
  });
});
