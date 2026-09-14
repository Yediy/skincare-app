import { resolveCameraPermissionState } from "@/analysis/camera-permission";

describe("resolveCameraPermissionState", () => {
  it("is unknown before permission has ever been queried", () => {
    expect(resolveCameraPermissionState(null, false)).toBe("unknown");
  });

  it("is requesting while a request is in flight, regardless of the last known response", () => {
    expect(resolveCameraPermissionState(null, true)).toBe("requesting");
    expect(resolveCameraPermissionState({ granted: false, canAskAgain: true }, true)).toBe("requesting");
  });

  it("is granted when the response says granted", () => {
    expect(resolveCameraPermissionState({ granted: true, canAskAgain: true }, false)).toBe("granted");
  });

  it("is denied (retryable) when denied but the OS will still show a prompt", () => {
    expect(resolveCameraPermissionState({ granted: false, canAskAgain: true }, false)).toBe("denied");
  });

  it("is blocked when denied and the OS will no longer prompt (must go to Settings)", () => {
    expect(resolveCameraPermissionState({ granted: false, canAskAgain: false }, false)).toBe("blocked");
  });
});
