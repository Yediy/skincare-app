import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PLACEHOLDER_HOSTS = new Set(["example.com", "www.example.com", "localhost", "127.0.0.1"]);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const IOS_BUNDLE_RE = /^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$/;
const ANDROID_PACKAGE_RE = /^[a-z][A-Za-z0-9_]*(?:\.[a-z][A-Za-z0-9_]*)+$/;

function nonBlank(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function isPlaceholder(value) {
  if (!nonBlank(value)) return true;
  const lower = value.trim().toLowerCase();
  return (
    lower.includes("your_") ||
    lower.includes("your-") ||
    lower.includes("changeme") ||
    lower.includes("example.com") ||
    lower.includes("placeholder")
  );
}

function validateHttpsUrl(env, name, errors) {
  const raw = env[name];
  if (!nonBlank(raw)) {
    errors.push(`${name} is required for a production release.`);
    return;
  }

  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    errors.push(`${name} must be a valid URL.`);
    return;
  }

  if (parsed.protocol !== "https:") {
    errors.push(`${name} must use https:// for a production release.`);
  }
  if (PLACEHOLDER_HOSTS.has(parsed.hostname.toLowerCase()) || parsed.hostname.toLowerCase().endsWith(".example.com")) {
    errors.push(`${name} must point to a real production host, not ${parsed.hostname}.`);
  }
}

function validateEasEnvironments(easConfig, errors) {
  const expected = {
    development: "development",
    preview: "preview",
    production: "production",
  };

  for (const [profile, environment] of Object.entries(expected)) {
    const actual = easConfig?.build?.[profile]?.environment;
    if (actual !== environment) {
      errors.push(`eas.json build.${profile}.environment must be "${environment}" (got ${JSON.stringify(actual)}).`);
    }
  }
}

export function validateReleaseReadiness({
  appConfig,
  easConfig,
  env,
  platform = "all",
}) {
  const errors = [];
  const expo = appConfig?.expo ?? appConfig ?? {};
  const wantsIos = platform === "all" || platform === "ios";
  const wantsAndroid = platform === "all" || platform === "android";

  if (!["all", "ios", "android"].includes(platform)) {
    errors.push(`Unsupported platform "${platform}". Use all, ios, or android.`);
    return errors;
  }

  if (!nonBlank(expo.name) || !nonBlank(expo.slug)) {
    errors.push("Expo app name and slug must both be configured.");
  }

  const projectId = expo?.extra?.eas?.projectId;
  if (!nonBlank(projectId) || !UUID_RE.test(projectId.trim())) {
    errors.push("expo.extra.eas.projectId must be a real EAS project UUID before release builds.");
  }

  if (wantsIos) {
    const bundleId = expo?.ios?.bundleIdentifier;
    if (!nonBlank(bundleId) || !IOS_BUNDLE_RE.test(bundleId.trim()) || bundleId.toLowerCase().includes("example")) {
      errors.push("expo.ios.bundleIdentifier must be a non-placeholder reverse-DNS identifier before iOS release.");
    }
  }

  if (wantsAndroid) {
    const packageName = expo?.android?.package;
    if (
      !nonBlank(packageName) ||
      !ANDROID_PACKAGE_RE.test(packageName.trim()) ||
      packageName.toLowerCase().includes("example")
    ) {
      errors.push("expo.android.package must be a non-placeholder Android application ID before Android release.");
    }
  }

  validateEasEnvironments(easConfig, errors);

  for (const name of [
    "EXPO_PUBLIC_API_BASE_URL",
    "EXPO_PUBLIC_PRIVACY_POLICY_URL",
    "EXPO_PUBLIC_TERMS_URL",
    "EXPO_PUBLIC_SUPPORT_URL",
    "EXPO_PUBLIC_ACCOUNT_DELETION_URL",
  ]) {
    validateHttpsUrl(env, name, errors);
  }

  if (env.EXPO_PUBLIC_REVENUECAT_ENABLED !== "true") {
    errors.push("EXPO_PUBLIC_REVENUECAT_ENABLED must be true for the subscription-enabled production release.");
  }

  if (!nonBlank(env.EXPO_PUBLIC_REVENUECAT_ENTITLEMENT_ID) || isPlaceholder(env.EXPO_PUBLIC_REVENUECAT_ENTITLEMENT_ID)) {
    errors.push("EXPO_PUBLIC_REVENUECAT_ENTITLEMENT_ID must be a real configured entitlement identifier.");
  }

  const checkProductionKey = (name) => {
    const value = env[name];
    if (!nonBlank(value) || isPlaceholder(value)) {
      errors.push(`${name} must be configured for the production store.`);
      return;
    }
    if (value.trim().toLowerCase().startsWith("test_")) {
      errors.push(`${name} is a RevenueCat Test Store key; production builds must use the platform-specific store key.`);
    }
  };

  if (wantsIos) checkProductionKey("EXPO_PUBLIC_REVENUECAT_IOS_API_KEY");
  if (wantsAndroid) checkProductionKey("EXPO_PUBLIC_REVENUECAT_ANDROID_API_KEY");

  return errors;
}

function parsePlatform(argv) {
  const argument = argv.find((value) => value.startsWith("--platform="));
  return argument ? argument.split("=")[1] : "all";
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function runCli() {
  const cwd = process.cwd();
  const appConfig = readJson(path.join(cwd, "app.json"));
  const easConfig = readJson(path.join(cwd, "eas.json"));
  const platform = parsePlatform(process.argv.slice(2));
  const errors = validateReleaseReadiness({ appConfig, easConfig, env: process.env, platform });

  if (errors.length > 0) {
    console.error("Release readiness check FAILED:");
    for (const error of errors) console.error(`- ${error}`);
    process.exitCode = 1;
    return;
  }

  console.log(`Release readiness check passed for platform: ${platform}`);
}

const isDirectRun =
  process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);

if (isDirectRun) runCli();
