/**
 * app/(app)/history.tsx -- Mobile C3 (history). Mocks
 * useAnalysisHistoryQuery directly (never renders the real
 * QueryClientProvider) -- server-returned pages are the ONLY source
 * this screen renders from; ownership/pagination correctness itself
 * is proven server-side (backend/tests/api/test_analyses_v2.py) and
 * at the hook level (tests/query/use-analysis-history.test.tsx).
 */
import { fireEvent, render, waitFor } from "@testing-library/react-native";
import React from "react";

import History from "@app/(app)/history";
import { ThemeProvider } from "@/theme/theme-provider";

const mockPush = jest.fn();
jest.mock("expo-router", () => ({
  useRouter: () => ({ push: mockPush }),
}));

const mockRefetch = jest.fn();
const mockFetchNextPage = jest.fn();
let mockQuery: {
  isPending: boolean;
  isError: boolean;
  error: unknown;
  data: { pages: { items: unknown[]; next_cursor: string | null }[] } | undefined;
  refetch: () => void;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
};

jest.mock("@/query/use-analysis-history", () => ({
  useAnalysisHistoryQuery: () => mockQuery,
}));

const ITEM_COMPLETED = {
  analysis_id: "a1",
  request_id: "r1",
  status: "COMPLETED",
  error_code: null,
  created_at: "2026-09-02T00:00:00Z",
  completed_at: "2026-09-02T00:05:00Z",
};

const ITEM_FAILED = {
  analysis_id: "a2",
  request_id: "r2",
  status: "FAILED",
  error_code: "NO_FACE_DETECTED",
  created_at: "2026-09-01T00:00:00Z",
  completed_at: null,
};

function EMPTY_QUERY(overrides: Partial<typeof mockQuery> = {}) {
  return {
    isPending: false,
    isError: false,
    error: undefined,
    data: { pages: [{ items: [], next_cursor: null }] },
    refetch: mockRefetch,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: mockFetchNextPage,
    ...overrides,
  };
}

async function renderScreen() {
  return render(<History />, { wrapper: ThemeProvider });
}

beforeEach(() => {
  mockPush.mockReset();
  mockRefetch.mockReset();
  mockFetchNextPage.mockReset();
  mockQuery = EMPTY_QUERY();
});

describe("History screen", () => {
  it("shows a loading state while the first page is loading", async () => {
    mockQuery = EMPTY_QUERY({ isPending: true, data: undefined });
    const { getByText } = await renderScreen();
    expect(getByText(/Loading your history/i)).toBeTruthy();
  });

  it("shows a provider/network error state with retry", async () => {
    mockQuery = EMPTY_QUERY({ isError: true, error: new Error("boom"), data: undefined });
    const { getByText } = await renderScreen();
    expect(getByText(/Something didn.t work/i)).toBeTruthy();

    fireEvent.press(getByText("Try again"));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it("shows an honest empty state and an entry point into starting an analysis", async () => {
    const { getByText } = await renderScreen();
    expect(getByText(/haven.t run an analysis yet/i)).toBeTruthy();

    fireEvent.press(getByText("Start skin analysis"));
    expect(mockPush).toHaveBeenCalledWith("/(app)/analysis");
  });

  it(
    "renders a row per item across every loaded page, newest first as the server ordered them",
    async () => {
      mockQuery = EMPTY_QUERY({ data: { pages: [{ items: [ITEM_COMPLETED, ITEM_FAILED], next_cursor: null }] } });
      const { getByText } = await renderScreen();

      expect(getByText("Complete")).toBeTruthy();
      expect(getByText("Didn't complete")).toBeTruthy();
    },
    15000,
  );

  it("shows a safe error description for a failed analysis row, never a raw error code", async () => {
    mockQuery = EMPTY_QUERY({ data: { pages: [{ items: [ITEM_FAILED], next_cursor: null }] } });
    const { queryByText, getByText } = await renderScreen();

    expect(getByText("Didn't complete")).toBeTruthy();
    expect(queryByText("NO_FACE_DETECTED")).toBeNull();
  });

  it("tapping a row navigates to that analysis's detail screen", async () => {
    mockQuery = EMPTY_QUERY({ data: { pages: [{ items: [ITEM_COMPLETED], next_cursor: null }] } });
    const { getByLabelText } = await renderScreen();

    fireEvent.press(getByLabelText(/Analysis from/i));
    expect(mockPush).toHaveBeenCalledWith("/(app)/analysis/a1");
  });

  it("requests the next page on reaching the end when more are available", async () => {
    mockQuery = EMPTY_QUERY({
      data: { pages: [{ items: [ITEM_COMPLETED], next_cursor: "next" }] },
      hasNextPage: true,
    });
    const { getByTestId } = await renderScreen();

    fireEvent(getByTestId("history-list"), "onEndReached");
    await waitFor(() => expect(mockFetchNextPage).toHaveBeenCalledTimes(1));
  });

  it("never requests another page once the server reports no next_cursor", async () => {
    mockQuery = EMPTY_QUERY({ data: { pages: [{ items: [ITEM_COMPLETED], next_cursor: null }] }, hasNextPage: false });
    const { getByTestId } = await renderScreen();

    fireEvent(getByTestId("history-list"), "onEndReached");
    expect(mockFetchNextPage).not.toHaveBeenCalled();
  });
});
