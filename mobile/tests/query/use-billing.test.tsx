import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react-native";
import React from "react";

import { useBillingStatusQuery, useBillingSyncMutation } from "@/query/use-billing";

const mockGetBillingStatus = jest.fn();
const mockSyncBillingStatus = jest.fn();
jest.mock("@/api/billing-api", () => ({
  getBillingStatus: () => mockGetBillingStatus(),
  syncBillingStatus: () => mockSyncBillingStatus(),
}));

// One QueryClient per test, unmounted afterward -- otherwise
// TanStack Query's own background timers keep the process alive past
// the test run ("Jest did not exit ... Consider --detectOpenHandles").
let queryClient: QueryClient;

function wrapper({ children }: { children: React.ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockGetBillingStatus.mockReset();
  mockSyncBillingStatus.mockReset();
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
});

afterEach(() => {
  queryClient.unmount();
});

const FREE_STATUS = {
  billing_enabled: false,
  app_user_id: "11111111-1111-1111-1111-111111111111",
  entitlement_identifier: "premium",
  environment: null,
  projection_status: null,
  has_premium_access: false,
  will_renew: null,
  expires_at: null,
  analysis_allowance: 3,
  period_key: "2026-09",
  analyses_used_or_reserved: 1,
  analyses_remaining: 2,
};

describe("useBillingStatusQuery", () => {
  it("fetches server billing status and exposes it verbatim (server allowance, not client-hardcoded)", async () => {
    mockGetBillingStatus.mockResolvedValue(FREE_STATUS);
    const { result } = await renderHook(() => useBillingStatusQuery({ enabled: true }), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(FREE_STATUS);
    expect(result.current.data?.analysis_allowance).toBe(3);
  });

  it("does not fetch when disabled", async () => {
    await renderHook(() => useBillingStatusQuery({ enabled: false }), { wrapper });
    expect(mockGetBillingStatus).not.toHaveBeenCalled();
  });
});

describe("useBillingSyncMutation", () => {
  it("calls syncBillingStatus and invalidates the billing status query on success", async () => {
    mockGetBillingStatus.mockResolvedValue(FREE_STATUS);
    mockSyncBillingStatus.mockResolvedValue({ ...FREE_STATUS, mismatch_found: false, corrected: false });

    const invalidateSpy = jest.spyOn(queryClient, "invalidateQueries");

    const { result } = await renderHook(() => useBillingSyncMutation(), { wrapper });
    await act(() => {
      result.current.mutate();
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockSyncBillingStatus).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ["billingStatus"] }));
  });
});
