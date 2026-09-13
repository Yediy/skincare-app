/* eslint-disable @typescript-eslint/no-require-imports -- dynamic
 * require() is required here: jest.resetModules() + a fresh require()
 * per test is the only way to re-evaluate config.ts's module-level
 * validation with different env/__DEV__ values each time; a static
 * import is evaluated once and can't be re-triggered. */
const dev = globalThis as unknown as { __DEV__: boolean };

describe("API_BASE_URL production HTTPS enforcement", () => {
  const originalDev = dev.__DEV__;
  const originalUrl = process.env.EXPO_PUBLIC_API_BASE_URL;

  afterEach(() => {
    dev.__DEV__ = originalDev;
    process.env.EXPO_PUBLIC_API_BASE_URL = originalUrl;
    jest.resetModules();
  });

  it("accepts an http:// origin in development", () => {
    jest.resetModules();
    dev.__DEV__ = true;
    process.env.EXPO_PUBLIC_API_BASE_URL = "http://localhost:8000";

    const { API_BASE_URL } = require("@/constants/config");
    expect(API_BASE_URL).toBe("http://localhost:8000");
  });

  it("throws if a production build's origin is not https://", () => {
    jest.resetModules();
    dev.__DEV__ = false;
    process.env.EXPO_PUBLIC_API_BASE_URL = "http://api.example.com";

    expect(() => require("@/constants/config")).toThrow(/https/i);
  });

  it("accepts an https:// origin in a production build", () => {
    jest.resetModules();
    dev.__DEV__ = false;
    process.env.EXPO_PUBLIC_API_BASE_URL = "https://api.example.com";

    const { API_BASE_URL } = require("@/constants/config");
    expect(API_BASE_URL).toBe("https://api.example.com");
  });

  it("throws when the env var is entirely unset", () => {
    jest.resetModules();
    delete process.env.EXPO_PUBLIC_API_BASE_URL;

    expect(() => require("@/constants/config")).toThrow(/EXPO_PUBLIC_API_BASE_URL/);
  });
});
