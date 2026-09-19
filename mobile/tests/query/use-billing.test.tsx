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

// One QueryClient per test, AND the rendered hook's component tree
// unmounted afterward (independent-review Blocker 5 root cause: this
// file previously called `queryClient.unmount()` but never the
// `unmount()` RTL's own `renderHook()` returns -- so the underlying
// React component/observer subscribing to the query was NEVER torn
// down. TanStack Query v5's default `gcTime` is 5 minutes; a query
// whose sole observer is still "mounted" from React's perspective
// keeps that observer's internal bookkeeping alive well past the test
// itself, which is exactly what kept the Jest process alive for
// several minutes after "Ran all test suites." -- confirmed directly
// by bisection: `tests/query/` was the ONLY subdirectory that
// reproduced the hang in isolation; unmounting the rendered tree here
// resolved it, with no `--forceExit` needed).
let queryClient: QueryClient;
let unmountHook: (() => void) | null = null;

function wrapper({ children }: { children: React.ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockGetBillingStatus.mockReset();
  mockSyncBillingStatus.mockReset();
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  unmountHook = null;
});

afterEach(() => {
  unmountHook?.();
  // Root cause of the independent-review Blocker 5 hang, found by
  // bisecting down to this exact file/test: every `Mutation` (and
  // `Query`) in TanStack Query v5 is a `Removable` that schedules its
  // own `setTimeout(..., gcTime)` (default 5 minutes) once created.
  // `queryCache.clear()` properly cancels each query's pending gc
  // timeout via `.remove()` -> `.destroy()`, but `mutationCache.clear()`
  // (installed/query-core's own source, confirmed by reading it
  // directly) only clears its internal bookkeeping Set -- it never
  // calls `.destroy()` on each mutation, so a mutation's own 5-minute
  // gc `setTimeout` survives `queryClient.clear()`/`unmount()`
  // entirely and keeps the Node process alive until it fires. Destroy
  // every mutation explicitly first.
  queryClient.getMutationCache().getAll().forEach((mutation) => mutation.destroy());
  queryClient.clear();
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
    const { result, unmount } = await renderHook(() => useBillingStatusQuery({ enabled: true }), { wrapper });
    unmountHook = unmount;

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(FREE_STATUS);
    expect(result.current.data?.analysis_allowance).toBe(3);
  });

  it("does not fetch when disabled", async () => {
    const { unmount } = await renderHook(() => useBillingStatusQuery({ enabled: false }), { wrapper });
    unmountHook = unmount;
    expect(mockGetBillingStatus).not.toHaveBeenCalled();
  });
});

describe("useBillingSyncMutation", () => {
  it("calls syncBillingStatus and invalidates the billing status query on success", async () => {
    mockGetBillingStatus.mockResolvedValue(FREE_STATUS);
    mockSyncBillingStatus.mockResolvedValue({ ...FREE_STATUS, mismatch_found: false, corrected: false });

    const invalidateSpy = jest.spyOn(queryClient, "invalidateQueries");

    const { result, unmount } = await renderHook(() => useBillingSyncMutation(), { wrapper });
    unmountHook = unmount;
    await act(() => {
      result.current.mutate();
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockSyncBillingStatus).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ["billingStatus"] }));
  });
});
