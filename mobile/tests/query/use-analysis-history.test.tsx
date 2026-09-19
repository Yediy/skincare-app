import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react-native";
import React from "react";

import { useAnalysisHistoryQuery } from "@/query/use-analysis-history";

const mockGetAnalysisHistory = jest.fn();
jest.mock("@/api/analysis-api", () => ({
  getAnalysisHistory: (params: unknown) => mockGetAnalysisHistory(params),
}));

// One QueryClient per test, and the rendered hook's own component
// tree unmounted afterward -- an un-unmounted observer can keep
// TanStack Query's internal bookkeeping subscribed past the test
// itself, so every test captures and calls the `unmount` renderHook()
// itself returns, not just clears/unmounts the client.
let queryClient: QueryClient;
let unmountHook: (() => void) | null = null;

function wrapper({ children }: { children: React.ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockGetAnalysisHistory.mockReset();
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  unmountHook = null;
});

afterEach(() => {
  unmountHook?.();
  queryClient.clear();
  queryClient.unmount();
});

const PAGE_1 = {
  items: [
    { analysis_id: "a1", request_id: "r1", status: "COMPLETED", created_at: "2026-09-02T00:00:00Z", completed_at: "2026-09-02T00:05:00Z" },
    { analysis_id: "a2", request_id: "r2", status: "FAILED", error_code: "NO_FACE_DETECTED", created_at: "2026-09-01T00:00:00Z", completed_at: null },
  ],
  next_cursor: "cursor-page-2",
};

const PAGE_2 = {
  items: [
    { analysis_id: "a3", request_id: "r3", status: "COMPLETED", created_at: "2026-08-31T00:00:00Z", completed_at: "2026-08-31T00:05:00Z" },
  ],
  next_cursor: null,
};

describe("useAnalysisHistoryQuery", () => {
  it("fetches the first page with no cursor", async () => {
    mockGetAnalysisHistory.mockResolvedValue(PAGE_1);
    const { result, unmount } = await renderHook(() => useAnalysisHistoryQuery({ enabled: true }), { wrapper });
    unmountHook = unmount;

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockGetAnalysisHistory).toHaveBeenCalledWith({ cursor: null });
    expect(result.current.data?.pages).toEqual([PAGE_1]);
  });

  it("does not fetch when disabled", async () => {
    const { unmount } = await renderHook(() => useAnalysisHistoryQuery({ enabled: false }), { wrapper });
    unmountHook = unmount;
    expect(mockGetAnalysisHistory).not.toHaveBeenCalled();
  });

  it("exposes hasNextPage from the server's next_cursor, and fetchNextPage requests the next page with it", async () => {
    mockGetAnalysisHistory.mockResolvedValueOnce(PAGE_1).mockResolvedValueOnce(PAGE_2);
    const { result, unmount } = await renderHook(() => useAnalysisHistoryQuery({ enabled: true }), { wrapper });
    unmountHook = unmount;

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.hasNextPage).toBe(true);

    await result.current.fetchNextPage();

    await waitFor(() => expect(result.current.data?.pages.length).toBe(2));
    expect(mockGetAnalysisHistory).toHaveBeenLastCalledWith({ cursor: "cursor-page-2" });
    expect(result.current.hasNextPage).toBe(false);
  });

  it("propagates a server error rather than silently showing an empty list", async () => {
    mockGetAnalysisHistory.mockRejectedValue(new Error("boom"));
    const { result, unmount } = await renderHook(() => useAnalysisHistoryQuery({ enabled: true }), { wrapper });
    unmountHook = unmount;

    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});
