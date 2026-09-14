/**
 * The one place this app writes to the console. Every log line is
 * redacted through here so a future call site can't accidentally
 * print a token, password, or sensitive profile field -- see
 * MOBILE_ARCHITECTURE.md's "Storage & logging policy" section.
 *
 * Never log: passwords, access tokens, refresh tokens, Authorization
 * headers, raw profile JSON, allergy/pregnancy/nursing values, or
 * (future) image data. REDACTED_KEYS below is deliberately
 * case-insensitive and substring-based, not an exhaustive exact list,
 * so a differently-cased or nested field of the same name is still
 * caught.
 */

const REDACTED_KEYS = [
  "password",
  "access_token",
  "accesstoken",
  "refresh_token",
  "refreshtoken",
  "authorization",
  "allerg",
  "pregnan",
  "nursing",
  "image",
  "photo",
  "base64",
];

function isRedactedKey(key: string): boolean {
  const lower = key.toLowerCase();
  return REDACTED_KEYS.some((needle) => lower.includes(needle));
}

function redact(value: unknown, depth = 0): unknown {
  if (depth > 5) return "[truncated]";
  if (value === null || value === undefined) return value;
  if (Array.isArray(value)) return value.map((v) => redact(v, depth + 1));
  if (typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, val] of Object.entries(value as Record<string, unknown>)) {
      out[key] = isRedactedKey(key) ? "[redacted]" : redact(val, depth + 1);
    }
    return out;
  }
  return value;
}

function format(args: unknown[]): unknown[] {
  return args.map((a) => (typeof a === "object" ? redact(a) : a));
}

export const logger = {
  debug(...args: unknown[]) {
    if (__DEV__) {
      // eslint-disable-next-line no-console
      console.log(...format(args));
    }
  },
  warn(...args: unknown[]) {
    console.warn(...format(args));
  },
  error(...args: unknown[]) {
    console.error(...format(args));
  },
};
