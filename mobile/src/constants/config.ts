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
