import {
  BACKOFF_AFTER_POLL_COUNT,
  BACKOFF_POLL_INTERVAL_MS,
  INITIAL_POLL_INTERVAL_MS,
  getPollingIntervalMs,
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

  it("completed/failed are terminal", () => {
    expect(isTerminalPhase("completed")).toBe(true);
    expect(isTerminalPhase("failed")).toBe(true);
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
