import { captureAttemptReducer } from "@/analysis/capture-attempt";

describe("captureAttemptReducer (section 1: request_id bound to the capture attempt)", () => {
  it("mints a fresh request_id for a newly captured photo", () => {
    const generateRequestId = jest.fn().mockReturnValue("id-1");

    const state = captureAttemptReducer(
      null,
      { type: "CAPTURED", uri: "file:///cache/a.jpg", width: 1600, height: 1200 },
      generateRequestId,
    );

    expect(state).toEqual({ uri: "file:///cache/a.jpg", width: 1600, height: 1200, requestId: "id-1" });
    expect(generateRequestId).toHaveBeenCalledTimes(1);
  });

  it("resubmitting the SAME captured photo does not change its request_id (no action dispatched between attempts)", () => {
    const generateRequestId = jest.fn().mockReturnValue("id-1");
    const state = captureAttemptReducer(
      null,
      { type: "CAPTURED", uri: "file:///cache/a.jpg", width: 1600, height: 1200 },
      generateRequestId,
    );

    // Two submission attempts of the same photo simply read
    // `state.requestId` twice -- nothing here mints a new one.
    expect(state?.requestId).toBe("id-1");
    expect(state?.requestId).toBe("id-1");
  });

  it("a new capture (Retake or a fresh photo) gets a DIFFERENT request_id than the previous one", () => {
    const generateRequestId = jest.fn().mockReturnValueOnce("id-1").mockReturnValueOnce("id-2");

    const first = captureAttemptReducer(
      null,
      { type: "CAPTURED", uri: "file:///cache/a.jpg", width: 1600, height: 1200 },
      generateRequestId,
    );
    const afterDiscard = captureAttemptReducer(first, { type: "DISCARDED" }, generateRequestId);
    const second = captureAttemptReducer(
      afterDiscard,
      { type: "CAPTURED", uri: "file:///cache/b.jpg", width: 1600, height: 1200 },
      generateRequestId,
    );

    expect(afterDiscard).toBeNull();
    expect(second?.requestId).toBe("id-2");
    expect(second?.requestId).not.toBe(first?.requestId);
  });

  it("two different captured photos can never share one request_id", () => {
    const generateRequestId = jest.fn().mockReturnValueOnce("id-1").mockReturnValueOnce("id-2");

    const photoA = captureAttemptReducer(
      null,
      { type: "CAPTURED", uri: "file:///cache/a.jpg", width: 1600, height: 1200 },
      generateRequestId,
    );
    const photoB = captureAttemptReducer(
      captureAttemptReducer(photoA, { type: "DISCARDED" }, generateRequestId),
      { type: "CAPTURED", uri: "file:///cache/b.jpg", width: 1600, height: 1200 },
      generateRequestId,
    );

    expect(photoA?.uri).not.toBe(photoB?.uri);
    expect(photoA?.requestId).not.toBe(photoB?.requestId);
  });

  it("server accepted Photo A but the client lost the response -> Retake -> Photo B uses a NEW request_id", () => {
    // Simulates: submit Photo A (server durably accepts it, but the
    // HTTP response never reaches this client) -> user taps Retake
    // (DISCARDED, no server interaction) -> captures Photo B. Photo
    // B's request_id must never equal Photo A's -- reusing it would
    // make the backend's idempotent-replay behavior return Photo A's
    // analysis for Photo B's submission.
    const generateRequestId = jest.fn().mockReturnValueOnce("photo-a-id").mockReturnValueOnce("photo-b-id");

    const photoA = captureAttemptReducer(
      null,
      { type: "CAPTURED", uri: "file:///cache/photo-a.jpg", width: 1600, height: 1200 },
      generateRequestId,
    );
    // Photo A's submission "succeeded on the server, lost in transit"
    // is invisible to this reducer -- from the screen's point of view
    // that's indistinguishable from a failed submission followed by
    // Retake, which is exactly the scenario this invariant must cover.
    const discarded = captureAttemptReducer(photoA, { type: "DISCARDED" }, generateRequestId);
    const photoB = captureAttemptReducer(
      discarded,
      { type: "CAPTURED", uri: "file:///cache/photo-b.jpg", width: 1600, height: 1200 },
      generateRequestId,
    );

    expect(photoA?.requestId).toBe("photo-a-id");
    expect(photoB?.requestId).toBe("photo-b-id");
    expect(photoB?.requestId).not.toBe(photoA?.requestId);
  });
});
