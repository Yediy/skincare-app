/**
 * src/billing/billing-action-readiness.ts -- independent-review
 * Blocker 2/"BILLING ACTION READINESS". Pure function, no mocking
 * needed: every named reason is exercised directly.
 */
import { computeBillingActionReadiness } from "@/billing/billing-action-readiness";
import type { BillingStatus } from "@/api/billing-api";

const USER_ID = "11111111-1111-1111-1111-111111111111";

function STATUS(overrides: Partial<BillingStatus> = {}): BillingStatus {
  return {
    billing_enabled: true,
    app_user_id: USER_ID,
    entitlement_identifier: "premium",
    environment: "SANDBOX",
    projection_status: null,
    has_premium_access: false,
    will_renew: null,
    expires_at: null,
    analysis_allowance: 3,
    period_key: "2026-09",
    analyses_used_or_reserved: 0,
    analyses_remaining: 3,
    ...overrides,
  };
}

const BASE = {
  mobileAvailability: "READY" as const,
  configuredUserId: USER_ID,
  mobileEntitlementId: "premium",
  serverStatus: STATUS(),
};

describe("computeBillingActionReadiness", () => {
  it("is READY when mobile availability, identity, and entitlement all agree with the server", () => {
    expect(computeBillingActionReadiness(BASE)).toBe("READY");
  });

  it("is MOBILE_UNAVAILABLE when native RevenueCat isn't READY, even if everything else matches", () => {
    expect(computeBillingActionReadiness({ ...BASE, mobileAvailability: "MISSING_KEY" })).toBe("MOBILE_UNAVAILABLE");
    expect(computeBillingActionReadiness({ ...BASE, mobileAvailability: "DISABLED" })).toBe("MOBILE_UNAVAILABLE");
    expect(computeBillingActionReadiness({ ...BASE, mobileAvailability: "WEB_UNSUPPORTED" })).toBe(
      "MOBILE_UNAVAILABLE",
    );
  });

  it("is SERVER_STATUS_UNAVAILABLE when server billing status hasn't loaded", () => {
    expect(computeBillingActionReadiness({ ...BASE, serverStatus: null })).toBe("SERVER_STATUS_UNAVAILABLE");
    expect(computeBillingActionReadiness({ ...BASE, serverStatus: undefined })).toBe("SERVER_STATUS_UNAVAILABLE");
  });

  it("is BACKEND_DISABLED when the server explicitly reports billing disabled, even if mobile is READY", () => {
    expect(
      computeBillingActionReadiness({ ...BASE, serverStatus: STATUS({ billing_enabled: false }) }),
    ).toBe("BACKEND_DISABLED");
  });

  it("is IDENTITY_NOT_CONFIGURED when RevenueCat has not yet identified this session's user", () => {
    expect(computeBillingActionReadiness({ ...BASE, configuredUserId: null })).toBe("IDENTITY_NOT_CONFIGURED");
  });

  it("is IDENTITY_MISMATCH when the configured RevenueCat user differs from the server's app_user_id", () => {
    expect(
      computeBillingActionReadiness({
        ...BASE,
        configuredUserId: "22222222-2222-2222-2222-222222222222",
      }),
    ).toBe("IDENTITY_MISMATCH");
  });

  it("is ENTITLEMENT_MISMATCH when the mobile and server entitlement identifiers disagree", () => {
    expect(
      computeBillingActionReadiness({ ...BASE, serverStatus: STATUS({ entitlement_identifier: "gold" }) }),
    ).toBe("ENTITLEMENT_MISMATCH");
  });

  it("checks mobile availability before consulting server status at all", () => {
    expect(
      computeBillingActionReadiness({ ...BASE, mobileAvailability: "DISABLED", serverStatus: null }),
    ).toBe("MOBILE_UNAVAILABLE");
  });

  it("checks backend-disabled before identity checks", () => {
    expect(
      computeBillingActionReadiness({
        ...BASE,
        configuredUserId: null,
        serverStatus: STATUS({ billing_enabled: false }),
      }),
    ).toBe("BACKEND_DISABLED");
  });
});
