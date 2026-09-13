import { ApiError } from "@/api/errors";
import { getConsentStatus, grantConsent, withdrawConsent } from "@/api/consent-api";
import { authorizedRequest } from "@/auth/auth-client-singleton";

jest.mock("@/auth/auth-client-singleton");

const mockAuthorizedRequest = authorizedRequest as jest.Mock;

describe("consent-api", () => {
  beforeEach(() => {
    mockAuthorizedRequest.mockReset();
  });

  it("getConsentStatus() reads backend truth, never a locally-cached boolean", async () => {
    const status = { consent_type: "facial_analysis", required_policy_version: "1.0", has_valid_consent: false };
    mockAuthorizedRequest.mockResolvedValue(status);

    await expect(getConsentStatus()).resolves.toEqual(status);
    expect(mockAuthorizedRequest).toHaveBeenCalledWith("/consent");
  });

  it("grantConsent() always sends the policy_version the caller supplies (sourced from GET /consent, never hardcoded)", async () => {
    mockAuthorizedRequest.mockResolvedValue({ id: "1", granted_at: "2026-01-01T00:00:00Z" });

    await grantConsent({ policy_version: "1.0", purpose: "facial skin analysis" });

    expect(mockAuthorizedRequest).toHaveBeenCalledWith("/consent", {
      method: "POST",
      body: { policy_version: "1.0", purpose: "facial skin analysis" },
    });
  });

  it("withdrawConsent() posts to /consent/withdraw", async () => {
    mockAuthorizedRequest.mockResolvedValue({ detail: "Consent withdrawn" });

    await withdrawConsent();

    expect(mockAuthorizedRequest).toHaveBeenCalledWith("/consent/withdraw", { method: "POST", body: {} });
  });

  it("propagates a 404 (nothing to withdraw) untouched", async () => {
    const error = new ApiError({ status: 404, code: "NOT_FOUND", message: "No active consent", retryable: false });
    mockAuthorizedRequest.mockRejectedValue(error);

    await expect(withdrawConsent()).rejects.toBe(error);
  });
});
