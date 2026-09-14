import { API_BASE_URL, REQUEST_TIMEOUT_MS } from "@/constants/config";
import { logger } from "@/utils/logger";

import { ApiError, classifyStatus, extractDetailMessage, genericMessageFor } from "./errors";

export type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "DELETE" | "PATCH";
  body?: unknown;
  headers?: Record<string, string>;
  /** Internal use by the refresh coordinator -- never set by a route
   * component directly. */
  signal?: AbortSignal;
};

/**
 * The one place this app calls fetch(). No route/component ever
 * issues a raw fetch() -- see MOBILE_ARCHITECTURE.md. Deliberately
 * has NO knowledge of access tokens or refresh -- that's
 * src/auth/refresh-coordinator.ts, which wraps this. Kept separate so
 * this layer stays trivially testable (no auth state to mock) and so
 * unauthenticated calls (signup/login/refresh itself) never
 * accidentally go through the refresh machinery.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(new Error("timeout")), REQUEST_TIMEOUT_MS);

  // Composing an externally-supplied signal (from the refresh
  // coordinator's retry-once logic) with our own timeout controller
  // so either can abort the request.
  const onExternalAbort = () => controller.abort(options.signal?.reason);
  options.signal?.addEventListener("abort", onExternalAbort);

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method: options.method ?? "GET",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        ...options.headers,
      },
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      signal: controller.signal,
    });
  } catch (err) {
    const aborted = err instanceof Error && err.name === "AbortError";
    const isTimeout = aborted && controller.signal.reason instanceof Error &&
      controller.signal.reason.message === "timeout";
    logger.warn("api request failed before a response", { path, isTimeout });
    throw new ApiError({
      status: null,
      code: isTimeout ? "TIMEOUT" : "NETWORK_ERROR",
      message: genericMessageFor(isTimeout ? "TIMEOUT" : "NETWORK_ERROR"),
      retryable: true,
    });
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", onExternalAbort);
  }

  const requestId = response.headers.get("x-request-id") ?? undefined;

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  let parsed: unknown = undefined;
  if (text.length > 0) {
    try {
      parsed = JSON.parse(text);
    } catch {
      if (response.ok) {
        throw new ApiError({
          status: response.status,
          code: "MALFORMED_RESPONSE",
          message: genericMessageFor("MALFORMED_RESPONSE"),
          requestId,
          retryable: false,
        });
      }
    }
  }

  if (!response.ok) {
    const code = classifyStatus(response.status);
    const retryAfterHeader = response.headers.get("retry-after");
    throw new ApiError({
      status: response.status,
      code,
      message: extractDetailMessage(parsed, genericMessageFor(code)),
      requestId,
      retryable: code === "RATE_LIMITED" || code === "SERVER_ERROR",
      retryAfterSeconds: retryAfterHeader ? Number(retryAfterHeader) : undefined,
    });
  }

  return parsed as T;
}
