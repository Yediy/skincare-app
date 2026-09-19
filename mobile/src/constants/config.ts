/**
 * Everything under EXPO_PUBLIC_* is bundled into the client and is
 * public application configuration -- it must never carry a secret.
 * Production must use HTTPS; this is enforced here, not just
 * documented, so a misconfigured production build fails loudly at
 * startup rather than silently sending credentials in the clear.
 */

const rawBaseUrl = process.env.EXPO_PUBLIC_API_BASE_URL;

if (!rawBaseUrl) {
  throw new Error(
    "EXPO_PUBLIC_API_BASE_URL is not set. Copy .env.example to .env and point it at your backend.",
  );
}

function assertProductionSafe(url: string): string {
  const isProdBuild = !__DEV__;
  if (isProdBuild && !url.startsWith("https://")) {
    throw new Error(
      `EXPO_PUBLIC_API_BASE_URL must be an https:// origin in a production build, got: ${url}`,
    );
  }
  return url;
}

export const API_BASE_URL = assertProductionSafe(rawBaseUrl.replace(/\/+$/, ""));

// A generous but finite request timeout -- distinguishes "the network
// is genuinely gone" from "waiting forever," which is what
// ApiErrorCode.TIMEOUT / network-error classification depends on.
export const REQUEST_TIMEOUT_MS = 15_000;

export const APP_VERSION = "1.0.0";

/**
 * Release URL seams (V1 account recovery / release-foundation pass,
 * Part 11). Public, non-secret configuration -- unlike
 * EXPO_PUBLIC_API_BASE_URL, every one of these is OPTIONAL in
 * development: a screen (Settings) simply omits a link whose URL
 * isn't configured, rather than throwing at startup. This pass does
 * not author any privacy policy/terms/support/account-deletion
 * content -- these are external URLs only; see
 * ACCOUNT_RECOVERY_ARCHITECTURE.md for what's still required before a
 * real store release (a production build's config validation is
 * expected to require all four, in a later pass, not this one).
 *
 * Production must use HTTPS if a URL is configured at all -- a
 * misconfigured non-https production value fails loudly here rather
 * than silently linking out over an insecure origin.
 */
function productionSafeOptionalUrl(raw: string | undefined): string | undefined {
  if (!raw) return undefined;
  if (!__DEV__ && !raw.startsWith("https://")) {
    throw new Error(`Release URL config must be an https:// origin in a production build, got: ${raw}`);
  }
  return raw;
}

export const PRIVACY_POLICY_URL = productionSafeOptionalUrl(process.env.EXPO_PUBLIC_PRIVACY_POLICY_URL);
export const TERMS_URL = productionSafeOptionalUrl(process.env.EXPO_PUBLIC_TERMS_URL);
export const SUPPORT_URL = productionSafeOptionalUrl(process.env.EXPO_PUBLIC_SUPPORT_URL);
export const ACCOUNT_DELETION_URL = productionSafeOptionalUrl(process.env.EXPO_PUBLIC_ACCOUNT_DELETION_URL);

/**
 * RevenueCat mobile configuration (Mobile C2). Every value here is a
 * PUBLIC RevenueCat mobile SDK key -- RevenueCat's own documented
 * distinction is that these are meant to ship inside a client binary,
 * unlike the backend-only secret REVENUECAT_API_KEY/
 * REVENUECAT_WEBHOOK_AUTH/REVENUECAT_WEBHOOK_SIGNING_SECRET (never
 * read by this app, never present in any EXPO_PUBLIC_* variable). All
 * optional, same posture as the release URLs above: an unconfigured
 * deployment (no key set, or REVENUECAT_ENABLED unset/false) must
 * still run normally -- C2's billing UI simply reports itself
 * unavailable rather than throwing at startup (see
 * src/billing/revenuecat-context.tsx).
 */
export const REVENUECAT_ENABLED = process.env.EXPO_PUBLIC_REVENUECAT_ENABLED === "true";
export const REVENUECAT_IOS_API_KEY = process.env.EXPO_PUBLIC_REVENUECAT_IOS_API_KEY;
export const REVENUECAT_ANDROID_API_KEY = process.env.EXPO_PUBLIC_REVENUECAT_ANDROID_API_KEY;
export const REVENUECAT_ENTITLEMENT_ID = process.env.EXPO_PUBLIC_REVENUECAT_ENTITLEMENT_ID || "premium";
