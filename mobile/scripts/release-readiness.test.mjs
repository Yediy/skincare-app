import test from "node:test";
import assert from "node:assert/strict";

import { validateReleaseReadiness } from "./release-readiness.mjs";

function validAppConfig() {
  return {
    expo: {
      name: "Skincare",
      slug: "skincare-mobile",
      ios: { bundleIdentifier: "com.acme.skincare" },
      android: { package: "com.acme.skincare" },
      extra: { eas: { projectId: "123e4567-e89b-42d3-a456-426614174000" } },
    },
  };
}

function validEasConfig() {
  return {
    build: {
      development: { environment: "development" },
      preview: { environment: "preview" },
      production: { environment: "production" },
    },
  };
}

function validEnv() {
  return {
    EXPO_PUBLIC_API_BASE_URL: "https://api.acme.test",
    EXPO_PUBLIC_PRIVACY_POLICY_URL: "https://acme.test/privacy",
    EXPO_PUBLIC_TERMS_URL: "https://acme.test/terms",
    EXPO_PUBLIC_SUPPORT_URL: "https://acme.test/support",
    EXPO_PUBLIC_ACCOUNT_DELETION_URL: "https://acme.test/delete-account",
    EXPO_PUBLIC_REVENUECAT_ENABLED: "true",
    EXPO_PUBLIC_REVENUECAT_ENTITLEMENT_ID: "premium",
    EXPO_PUBLIC_REVENUECAT_IOS_API_KEY: "appl_real_public_key",
    EXPO_PUBLIC_REVENUECAT_ANDROID_API_KEY: "goog_real_public_key",
  };
}

test("valid production configuration passes", () => {
  assert.deepEqual(
    validateReleaseReadiness({
      appConfig: validAppConfig(),
      easConfig: validEasConfig(),
      env: validEnv(),
      platform: "all",
    }),
    [],
  );
});

test("missing permanent identifiers and EAS project identity fail closed", () => {
  const appConfig = validAppConfig();
  delete appConfig.expo.ios.bundleIdentifier;
  delete appConfig.expo.android.package;
  delete appConfig.expo.extra.eas.projectId;

  const errors = validateReleaseReadiness({
    appConfig,
    easConfig: validEasConfig(),
    env: validEnv(),
  });

  assert.ok(errors.some((value) => value.includes("bundleIdentifier")));
  assert.ok(errors.some((value) => value.includes("android.package")));
  assert.ok(errors.some((value) => value.includes("projectId")));
});

test("production URLs must be HTTPS and non-placeholder", () => {
  const env = validEnv();
  env.EXPO_PUBLIC_PRIVACY_POLICY_URL = "http://example.com/privacy";
  env.EXPO_PUBLIC_ACCOUNT_DELETION_URL = "https://localhost/delete";

  const errors = validateReleaseReadiness({
    appConfig: validAppConfig(),
    easConfig: validEasConfig(),
    env,
  });

  assert.ok(errors.some((value) => value.includes("PRIVACY_POLICY_URL") && value.includes("https://")));
  assert.ok(errors.some((value) => value.includes("PRIVACY_POLICY_URL") && value.includes("real production host")));
  assert.ok(errors.some((value) => value.includes("ACCOUNT_DELETION_URL") && value.includes("real production host")));
});

test("subscription release requires RevenueCat production configuration", () => {
  const env = validEnv();
  env.EXPO_PUBLIC_REVENUECAT_ENABLED = "false";
  env.EXPO_PUBLIC_REVENUECAT_IOS_API_KEY = "test_development_key";
  delete env.EXPO_PUBLIC_REVENUECAT_ANDROID_API_KEY;

  const errors = validateReleaseReadiness({
    appConfig: validAppConfig(),
    easConfig: validEasConfig(),
    env,
  });

  assert.ok(errors.some((value) => value.includes("REVENUECAT_ENABLED")));
  assert.ok(errors.some((value) => value.includes("Test Store key")));
  assert.ok(errors.some((value) => value.includes("ANDROID_API_KEY")));
});

test("platform-scoped checks do not require the other platform identifier or key", () => {
  const appConfig = validAppConfig();
  const env = validEnv();
  delete appConfig.expo.android.package;
  delete env.EXPO_PUBLIC_REVENUECAT_ANDROID_API_KEY;

  const errors = validateReleaseReadiness({
    appConfig,
    easConfig: validEasConfig(),
    env,
    platform: "ios",
  });

  assert.deepEqual(errors, []);
});

test("EAS profiles must bind explicitly to matching environments", () => {
  const easConfig = validEasConfig();
  delete easConfig.build.production.environment;

  const errors = validateReleaseReadiness({
    appConfig: validAppConfig(),
    easConfig,
    env: validEnv(),
  });

  assert.ok(errors.some((value) => value.includes("build.production.environment")));
});
