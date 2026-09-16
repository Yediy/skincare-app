import { ApiError } from "@/api/errors";
import {
  BACKOFF_AFTER_POLL_COUNT,
  BACKOFF_POLL_INTERVAL_MS,
  INITIAL_POLL_INTERVAL_MS,
  derivePollingOutcome,
  getPollingIntervalMs,
  isPermanentPollingError,
  isTerminalPhase,
  mapBackendStatusToPhase,
} from "@/analysis/analysis-lifecycle";
import type { AnalysisRequestStatus } from "@/types/domain";

describe("mapBackendStatusToPhase", () => {
  it.each([
    ["RECEIVED", "queued"],
    ["QUEUED", "queued"],
    ["PROCESSING", "processing"],
    ["COMPLETED", "completed"],
    ["FAILED", "failed"],
    ["CANCELLED", "failed"],
  ] satisfies [AnalysisRequestStatus, string][])("maps backend status %s to phase %s", (status, phase) => {
    expect(mapBackendStatusToPhase(status)).toBe(phase);
  });
});

describe("isTerminalPhase", () => {
  it("queued/processing/draft/submitting are not terminal", () => {
    expect(isTerminalPhase("queued")).toBe(false);
    expect(isTerminalPhase("processing")).toBe(false);
    expect(isTerminalPhase("draft")).toBe(false);
    expect(isTerminalPhase("submitting")).toBe(false);
  });

  it("completed/failed/unavailable are terminal", () => {
    expect(isTerminalPhase("completed")).toBe(true);
    expect(isTerminalPhase("failed")).toBe(true);
    expect(isTerminalPhase("unavailable")).toBe(true);
  });
});

describe("isPermanentPollingError (section 8)", () => {
  it("is true for a nonretryable ApiError (404/403/other 4xx)", () => {
    expect(isPermanentPollingError(new ApiError({ status: 404, code: "NOT_FOUND", message: "x", retryable: false }))).toBe(true);
    expect(isPermanentPollingError(new ApiError({ status: 403, code: "FORBIDDEN", message: "x", retryable: false }))).toBe(true);
  });

  it("is false for a retryable ApiError (network/timeout/5xx/429)", () => {
    expect(isPermanentPollingError(new ApiError({ status: null, code: "NETWORK_ERROR", message: "x", retryable: true }))).toBe(false);
    expect(isPermanentPollingError(new ApiError({ status: 500, code: "SERVER_ERROR", message: "x", retryable: true }))).toBe(false);
    expect(isPermanentPollingError(new ApiError({ status: 429, code: "RATE_LIMITED", message: "x", retryable: true }))).toBe(false);
  });

  it("is false for no error at all", () => {
    expect(isPermanentPollingError(null)).toBe(false);
    expect(isPermanentPollingError(undefined)).toBe(false);
  });
});

describe("derivePollingOutcome (section 8: the hook's full phase/isPollingError decision, extracted pure)", () => {
  it("an initial 404 (nonretryable, no data ever fetched) stops polling and surfaces 'unavailable', not 'queued'", () => {
    const error = new ApiError({ status: 404, code: "NOT_FOUND", message: "x", retryable: false });
    expect(derivePollingOutcome(undefined, error, true)).toEqual({ phase: "unavailable", isPollingError: false });
  });

  it("a nonretryable 4xx (e.g. 403) stops polling and surfaces 'unavailable'", () => {
    const error = new ApiError({ status: 403, code: "FORBIDDEN", message: "x", retryable: false });
    expect(derivePollingOutcome(undefined, error, true)).toEqual({ phase: "unavailable", isPollingError: false });
  });

  it("a network error (no data yet) is transient -- reports isPollingError, never 'unavailable', defaults to 'queued'", () => {
    const error = new ApiError({ status: null, code: "NETWORK_ERROR", message: "x", retryable: true });
    expect(derivePollingOutcome(undefined, error, true)).toEqual({ phase: "queued", isPollingError: true });
  });

  it("a 5xx (no data yet) is transient -- reports isPollingError, never 'unavailable'", () => {
    const error = new ApiError({ status: 500, code: "SERVER_ERROR", message: "x", retryable: true });
    expect(derivePollingOutcome(undefined, error, true)).toEqual({ phase: "queued", isPollingError: true });
  });

  it("a failed background poll never converts a known PROCESSING analysis into 'failed' or 'unavailable'", () => {
    const transientError = new ApiError({ status: null, code: "TIMEOUT", message: "x", retryable: true });
    const lastKnownGood = { status: "PROCESSING" as const };

    expect(derivePollingOutcome(lastKnownGood, transientError, true)).toEqual({
      phase: "processing",
      isPollingError: true,
    });
  });

  it("a PERMANENT error arriving after previously-known-good data still surfaces 'unavailable', not the stale phase", () => {
    const permanentError = new ApiError({ status: 404, code: "NOT_FOUND", message: "x", retryable: false });
    const lastKnownGood = { status: "PROCESSING" as const };

    expect(derivePollingOutcome(lastKnownGood, permanentError, true)).toEqual({
      phase: "unavailable",
      isPollingError: false,
    });
  });

  it("a terminal COMPLETED analysis has no error and reports phase 'completed'", () => {
    expect(derivePollingOutcome({ status: "COMPLETED" }, null, false)).toEqual({
      phase: "completed",
      isPollingError: false,
    });
  });

  it("a terminal FAILED analysis reports phase 'failed', not 'unavailable' -- a real backend outcome, not a polling failure", () => {
    expect(derivePollingOutcome({ status: "FAILED" }, null, false)).toEqual({
      phase: "failed",
      isPollingError: false,
    });
  });

  it("no error and no data yet defaults to 'queued'", () => {
    expect(derivePollingOutcome(undefined, null, false)).toEqual({ phase: "queued", isPollingError: false });
  });
});

describe("getPollingIntervalMs", () => {
  it("continues polling while queued", () => {
    expect(getPollingIntervalMs("queued", 0)).toBe(INITIAL_POLL_INTERVAL_MS);
  });

  it("continues polling while processing", () => {
    expect(getPollingIntervalMs("processing", 0)).toBe(INITIAL_POLL_INTERVAL_MS);
  });

  it("stops polling once completed", () => {
    expect(getPollingIntervalMs("completed", 1)).toBe(false);
  });

  it("stops polling once failed", () => {
    expect(getPollingIntervalMs("failed", 1)).toBe(false);
  });

  it("does not poll every 100ms -- the initial interval is at least a second", () => {
    expect(INITIAL_POLL_INTERVAL_MS).toBeGreaterThanOrEqual(1000);
  });

  it("backs off to a longer interval after many polls without reaching a terminal state", () => {
    expect(getPollingIntervalMs("processing", BACKOFF_AFTER_POLL_COUNT)).toBe(BACKOFF_POLL_INTERVAL_MS);
    expect(getPollingIntervalMs("processing", BACKOFF_AFTER_POLL_COUNT - 1)).toBe(INITIAL_POLL_INTERVAL_MS);
  });
});
