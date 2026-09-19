/**
 * app/(app)/subscription.tsx -- Mobile C2 Part 11/20. Mocks
 * useBillingStatusQuery/useBillingSyncMutation and useRevenueCat
 * directly (never renders the real providers) -- server status is
 * the ONLY source this screen renders premium/allowance state from;
 * these tests prove that, and that a local CustomerInfo/paywall
 * signal never flips premium UI on its own.
 */
import { fireEvent, render, waitFor } from "@testing-library/react-native";
import React from "react";
import { Platform } from "react-native";

import Subscription from "@app/(app)/subscription";
import { PAYWALL_RESULT } from "@/billing/revenuecat-adapter";
import { ThemeProvider } from "@/theme/theme-provider";

const mockPresentPaywall = jest.fn();
const mockRestorePurchases = jest.fn();
const mockPresentCustomerCenter = jest.fn();
let mockAvailability = "READY";

let mockConfiguredUserId: string | null = "11111111-1111-1111-1111-111111111111";
let mockEntitlementId = "premium";
const mockRetryIdentification = jest.fn();

jest.mock("@/billing/revenuecat-context", () => ({
  useRevenueCat: () => ({
    availability: mockAvailability,
    entitlementId: mockEntitlementId,
    configuredUserId: mockConfiguredUserId,
    presentPaywall: () => mockPresentPaywall(),
    restorePurchases: () => mockRestorePurchases(),
    presentCustomerCenter: () => mockPresentCustomerCenter(),
    retryIdentification: () => mockRetryIdentification(),
  }),
}));

const mockRefetch = jest.fn();
let mockStatusQuery: {
  isPending: boolean;
  isError: boolean;
  error: unknown;
  data: Record<string, unknown> | undefined;
  refetch: () => void;
};
const mockMutateAsync = jest.fn();
let mockSyncMutation: { mutateAsync: () => Promise<unknown>; isPending: boolean; isError: boolean };

jest.mock("@/query/use-billing", () => ({
  useBillingStatusQuery: () => mockStatusQuery,
  useBillingSyncMutation: () => mockSyncMutation,
}));

function FREE_STATUS(overrides: Record<string, unknown> = {}) {
  return {
    billing_enabled: true,
    app_user_id: "11111111-1111-1111-1111-111111111111",
    entitlement_identifier: "premium",
    environment: "SANDBOX",
    projection_status: null,
    has_premium_access: false,
    will_renew: null,
    expires_at: null,
    analysis_allowance: 3,
    period_key: "2026-09",
    analyses_used_or_reserved: 1,
    analyses_remaining: 2,
    ...overrides,
  };
}

function PREMIUM_STATUS(overrides: Record<string, unknown> = {}) {
  return FREE_STATUS({
    projection_status: "ACTIVE",
    has_premium_access: true,
    will_renew: true,
    expires_at: "2026-10-18T00:00:00Z",
    analysis_allowance: 100,
    analyses_used_or_reserved: 4,
    analyses_remaining: 96,
    ...overrides,
  });
}

async function renderScreen() {
  return render(<Subscription />, { wrapper: ThemeProvider });
}

beforeEach(() => {
  mockAvailability = "READY";
  mockConfiguredUserId = "11111111-1111-1111-1111-111111111111";
  mockEntitlementId = "premium";
  mockPresentPaywall.mockReset();
  mockRestorePurchases.mockReset();
  mockPresentCustomerCenter.mockReset().mockResolvedValue(undefined);
  mockRetryIdentification.mockReset();
  mockRefetch.mockReset();
  mockMutateAsync.mockReset().mockResolvedValue(undefined);
  mockStatusQuery = { isPending: true, isError: false, error: undefined, data: undefined, refetch: mockRefetch };
  mockSyncMutation = { mutateAsync: mockMutateAsync, isPending: false, isError: false };
  Platform.OS = "ios";
});

describe("Subscription screen", () => {
  it("shows a loading state while server entitlement is loading", async () => {
    const { getByText } = await renderScreen();
    expect(getByText(/Loading your subscription status/i)).toBeTruthy();
  });

  it("shows a provider/network error state with retry", async () => {
    mockStatusQuery = { isPending: false, isError: true, error: new Error("boom"), data: undefined, refetch: mockRefetch };
    const { getByText } = await renderScreen();
    expect(getByText(/Something didn.t work/i)).toBeTruthy();
  });

  it("shows the free tier with server-reported allowance, never a client-hardcoded number", async () => {
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS({ analysis_allowance: 7, analyses_remaining: 5 }), refetch: mockRefetch };
    const { getByText, queryByText } = await renderScreen();
    expect(getByText(/5 of 7 free analyses remaining/i)).toBeTruthy();
    expect(getByText("Upgrade to Premium")).toBeTruthy();
    expect(queryByText("Manage subscription")).toBeNull();
  });

  it("shows premium active state with server-reported allowance and renewal info", async () => {
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: PREMIUM_STATUS(), refetch: mockRefetch };
    const { getByText, queryByText } = await renderScreen();
    expect(getByText("You have premium access.")).toBeTruthy();
    expect(getByText(/96 of 100 analyses remaining/i)).toBeTruthy();
    expect(getByText(/Renews automatically/i)).toBeTruthy();
    expect(getByText("Manage subscription")).toBeTruthy();
    expect(queryByText("Upgrade to Premium")).toBeNull();
  });

  it("shows a distinct grace-period message while still granting premium display", async () => {
    mockStatusQuery = {
      isPending: false, isError: false, error: undefined, refetch: mockRefetch,
      data: PREMIUM_STATUS({ projection_status: "GRACE_PERIOD" }),
    };
    const { getByText } = await renderScreen();
    expect(getByText(/grace period/i)).toBeTruthy();
  });

  it("shows RevenueCat-unavailable state and hides purchase actions when availability isn't READY", async () => {
    mockAvailability = "MISSING_KEY";
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS(), refetch: mockRefetch };
    const { getByText, queryByText } = await renderScreen();
    expect(getByText(/Purchases unavailable/i)).toBeTruthy();
    expect(queryByText("Upgrade to Premium")).toBeNull();
    expect(queryByText("Restore purchases")).toBeNull();
  });

  it("renders an honest unsupported state on web and never calls native purchase/restore", async () => {
    Platform.OS = "web";
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS(), refetch: mockRefetch };
    const { getByText } = await renderScreen();
    expect(getByText(/isn.t available on web/i)).toBeTruthy();
    expect(mockPresentPaywall).not.toHaveBeenCalled();
    expect(mockRestorePurchases).not.toHaveBeenCalled();
  });

  it("purchase cancelled is not an error -- no sync triggered, no error shown", async () => {
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS(), refetch: mockRefetch };
    mockPresentPaywall.mockResolvedValue(PAYWALL_RESULT.CANCELLED);
    const { getByText, queryByText } = await renderScreen();

    fireEvent.press(getByText("Upgrade to Premium"));
    await waitFor(() => expect(mockPresentPaywall).toHaveBeenCalledTimes(1));

    expect(mockMutateAsync).not.toHaveBeenCalled();
    expect(queryByText(/Something went wrong with your purchase/i)).toBeNull();
  });

  it("purchase succeeded triggers a backend sync (never unlocks premium locally)", async () => {
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS(), refetch: mockRefetch };
    mockPresentPaywall.mockResolvedValue(PAYWALL_RESULT.PURCHASED);
    const { getByText } = await renderScreen();

    fireEvent.press(getByText("Upgrade to Premium"));

    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));
    // The screen never flips a local "hasPremium" flag -- it still
    // renders exactly what mockStatusQuery.data says (free), proving
    // premium UI is derived only from the server query, not from the
    // paywall result.
    expect(getByText("Upgrade to Premium")).toBeTruthy();
  });

  it("purchase succeeded but backend sync fails -- premium is NOT unlocked locally, sync-failed UI shown", async () => {
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS(), refetch: mockRefetch };
    mockPresentPaywall.mockResolvedValue(PAYWALL_RESULT.PURCHASED);
    mockMutateAsync.mockRejectedValue(new Error("sync failed"));
    mockSyncMutation = { mutateAsync: mockMutateAsync, isPending: false, isError: true };
    const { getByText } = await renderScreen();

    fireEvent.press(getByText("Upgrade to Premium"));

    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(getByText(/couldn.t confirm your purchase/i)).toBeTruthy());
    // Still free-tier UI -- never unlocked client-side as a workaround.
    expect(getByText("Upgrade to Premium")).toBeTruthy();
  });

  it("restore succeeded triggers a backend sync", async () => {
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS(), refetch: mockRefetch };
    mockRestorePurchases.mockResolvedValue({ entitlements: { active: {} } });
    const { getByText } = await renderScreen();

    fireEvent.press(getByText("Restore purchases"));

    await waitFor(() => expect(mockRestorePurchases).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));
  });

  it("Customer Center closing triggers a status refresh via sync", async () => {
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: PREMIUM_STATUS(), refetch: mockRefetch };
    const { getByText } = await renderScreen();

    fireEvent.press(getByText("Manage subscription"));

    await waitFor(() => expect(mockPresentCustomerCenter).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));
  });

  it("Customer Center failing shows a bounded error, does not sync, and does not change premium state", async () => {
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: PREMIUM_STATUS(), refetch: mockRefetch };
    mockPresentCustomerCenter.mockRejectedValue(new Error("Customer Center failed"));
    const { getByText } = await renderScreen();

    fireEvent.press(getByText("Manage subscription"));

    await waitFor(() => expect(mockPresentCustomerCenter).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(getByText(/Something went wrong with your purchase/i)).toBeTruthy());
    // Customer Center never successfully closed -- no sync attempted.
    expect(mockMutateAsync).not.toHaveBeenCalled();
    // Still premium (unchanged) -- a Customer Center failure never
    // revokes or alters displayed access.
    expect(getByText("You have premium access.")).toBeTruthy();
  });

  it("independent-review Blocker 3: paywall NOT_PRESENTED is treated as evidence of stale local state, not cancellation -- triggers a backend sync", async () => {
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS(), refetch: mockRefetch };
    mockPresentPaywall.mockResolvedValue(PAYWALL_RESULT.NOT_PRESENTED);
    const { getByText } = await renderScreen();

    fireEvent.press(getByText("Upgrade to Premium"));

    await waitFor(() => expect(mockPresentPaywall).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));
    // Server status remains the sole authority -- the mocked query
    // still says free, so the screen still renders free-tier UI.
    expect(getByText("Upgrade to Premium")).toBeTruthy();
  });

  it("true user cancellation (CANCELLED) never triggers a sync, unlike NOT_PRESENTED", async () => {
    mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS(), refetch: mockRefetch };
    mockPresentPaywall.mockResolvedValue(PAYWALL_RESULT.CANCELLED);
    const { getByText } = await renderScreen();

    fireEvent.press(getByText("Upgrade to Premium"));

    await waitFor(() => expect(mockPresentPaywall).toHaveBeenCalledTimes(1));
    expect(mockMutateAsync).not.toHaveBeenCalled();
  });

  describe("independent-review Blocker 2: purchase-action readiness gate", () => {
    it("hides purchase actions when the server reports billing disabled, even though mobile is READY", async () => {
      mockStatusQuery = {
        isPending: false, isError: false, error: undefined, refetch: mockRefetch,
        data: FREE_STATUS({ billing_enabled: false }),
      };
      const { getByText, queryByText } = await renderScreen();

      expect(getByText(/2 of 3 free analyses remaining/i)).toBeTruthy();
      expect(queryByText("Upgrade to Premium")).toBeNull();
      expect(queryByText("Restore purchases")).toBeNull();
      expect(getByText(/Purchases unavailable/i)).toBeTruthy();
    });

    it("hides purchase actions when the configured RevenueCat user differs from the server's app_user_id", async () => {
      mockConfiguredUserId = "99999999-9999-9999-9999-999999999999";
      mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS(), refetch: mockRefetch };
      const { getByText, queryByText } = await renderScreen();

      expect(queryByText("Upgrade to Premium")).toBeNull();
      expect(queryByText("Restore purchases")).toBeNull();
      expect(getByText(/Purchases unavailable/i)).toBeTruthy();
      expect(mockPresentPaywall).not.toHaveBeenCalled();
    });

    it("hides purchase actions when the mobile and server entitlement identifiers disagree", async () => {
      mockEntitlementId = "gold";
      mockStatusQuery = {
        isPending: false, isError: false, error: undefined, refetch: mockRefetch,
        data: FREE_STATUS({ entitlement_identifier: "premium" }),
      };
      const { getByText, queryByText } = await renderScreen();

      expect(queryByText("Upgrade to Premium")).toBeNull();
      expect(getByText(/Purchases unavailable/i)).toBeTruthy();
    });

    it("offers a Retry action when RevenueCat identification hasn't completed yet, wired to retryIdentification()", async () => {
      mockConfiguredUserId = null;
      mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS(), refetch: mockRefetch };
      const { getByText } = await renderScreen();

      expect(getByText(/Purchases unavailable/i)).toBeTruthy();
      fireEvent.press(getByText("Retry"));
      expect(mockRetryIdentification).toHaveBeenCalledTimes(1);
    });

    it("existing premium access is always displayed even when purchase actions are unavailable", async () => {
      mockStatusQuery = {
        isPending: false, isError: false, error: undefined, refetch: mockRefetch,
        data: PREMIUM_STATUS({ billing_enabled: false }),
      };
      const { getByText, queryByText } = await renderScreen();

      expect(getByText("You have premium access.")).toBeTruthy();
      expect(queryByText("Manage subscription")).toBeNull();
    });

    it("enables ordinary purchase actions when mobile availability, identity, and entitlement all match the server", async () => {
      mockStatusQuery = { isPending: false, isError: false, error: undefined, data: FREE_STATUS(), refetch: mockRefetch };
      const { getByText } = await renderScreen();

      expect(getByText("Upgrade to Premium")).toBeTruthy();
      expect(getByText("Restore purchases")).toBeTruthy();
    });
  });
});
