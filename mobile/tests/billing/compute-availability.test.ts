/**
 * src/billing/revenuecat-availability.ts::computeAvailability() --
 * pure derivation of DISABLED/WEB_UNSUPPORTED/MISSING_KEY/READY.
 * Deliberately its own dependency-free module (no auth/session/native-
 * SDK imports) specifically so this is testable via a fresh
 * `jest.doMock("@/constants/config", ...)`/`jest.doMock("react-native", ...)`
 * per scenario without needing to stub the rest of the app's import
 * graph (expo-secure-store, react-native-purchases, etc.).
 */

function loadComputeAvailability(overrides: {
  enabled: boolean;
  platform: "ios" | "android" | "web";
  iosKey?: string;
  androidKey?: string;
}): string {
  let result = "";
  jest.isolateModules(() => {
    jest.doMock("@/constants/config", () => ({
      REVENUECAT_ENABLED: overrides.enabled,
      REVENUECAT_IOS_API_KEY: overrides.iosKey,
      REVENUECAT_ANDROID_API_KEY: overrides.androidKey,
      REVENUECAT_ENTITLEMENT_ID: "premium",
    }));
    jest.doMock("react-native", () => ({ Platform: { OS: overrides.platform } }));
    const mod = require("@/billing/revenuecat-availability");
    result = mod.computeAvailability();
  });
  return result;
}

describe("computeAvailability", () => {
  it("is DISABLED when REVENUECAT_ENABLED is false, regardless of platform/keys", () => {
    expect(loadComputeAvailability({ enabled: false, platform: "ios", iosKey: "k" })).toBe("DISABLED");
  });

  it("is WEB_UNSUPPORTED on web even when enabled and a key exists", () => {
    expect(loadComputeAvailability({ enabled: true, platform: "web", iosKey: "k", androidKey: "k" })).toBe(
      "WEB_UNSUPPORTED",
    );
  });

  it("is MISSING_KEY on iOS when enabled but no iOS key is configured", () => {
    expect(loadComputeAvailability({ enabled: true, platform: "ios", androidKey: "k" })).toBe("MISSING_KEY");
  });

  it("is MISSING_KEY on Android when enabled but no Android key is configured", () => {
    expect(loadComputeAvailability({ enabled: true, platform: "android", iosKey: "k" })).toBe("MISSING_KEY");
  });

  it("is READY on iOS when enabled with an iOS key", () => {
    expect(loadComputeAvailability({ enabled: true, platform: "ios", iosKey: "k" })).toBe("READY");
  });

  it("is READY on Android when enabled with an Android key", () => {
    expect(loadComputeAvailability({ enabled: true, platform: "android", androidKey: "k" })).toBe("READY");
  });
});
