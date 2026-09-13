/**
 * The backend (backend/app/main.py) has no `code`/`request_id`
 * convention on its HTTP error bodies -- every error is either
 * `{"detail": "<message>"}` or, for 422 validation, FastAPI's default
 * `{"detail": [{"loc": [...], "msg": ..., "type": ...}, ...]}`. So
 * `code` here is classified client-side from the status code (and,
 * for /login specifically, from context) -- it is never parsed out of
 * the response body, and the response body is never surfaced to the
 * user verbatim.
 */
export type ApiErrorCode =
  | "NETWORK_ERROR"
  | "TIMEOUT"
  | "UNAUTHORIZED"
  | "FORBIDDEN"
  | "NOT_FOUND"
  | "CONFLICT"
  | "VALIDATION_ERROR"
  | "RATE_LIMITED"
  | "SERVER_ERROR"
  | "MALFORMED_RESPONSE"
  | "UNKNOWN";

export type ApiErrorInit = {
  status: number | null;
  code: ApiErrorCode;
  message: string;
  requestId?: string;
  retryable: boolean;
  retryAfterSeconds?: number;
};

export class ApiError extends Error {
  readonly status: number | null;
  readonly code: ApiErrorCode;
  readonly requestId?: string;
  readonly retryable: boolean;
  readonly retryAfterSeconds?: number;

  constructor(init: ApiErrorInit) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.requestId = init.requestId;
    this.retryable = init.retryable;
    this.retryAfterSeconds = init.retryAfterSeconds;
  }
}

const GENERIC_MESSAGES: Record<ApiErrorCode, string> = {
  NETWORK_ERROR: "You appear to be offline. Check your connection and try again.",
  TIMEOUT: "That took too long to respond. Please try again.",
  UNAUTHORIZED: "Your session has expired. Please sign in again.",
  FORBIDDEN: "You don't have permission to do that.",
  NOT_FOUND: "We couldn't find what you were looking for.",
  CONFLICT: "That already exists.",
  VALIDATION_ERROR: "Please check the information you entered.",
  RATE_LIMITED: "Too many attempts. Please wait a moment and try again.",
  SERVER_ERROR: "Something went wrong on our end. Please try again shortly.",
  MALFORMED_RESPONSE: "We received an unexpected response. Please try again.",
  UNKNOWN: "Something went wrong. Please try again.",
};

export function genericMessageFor(code: ApiErrorCode): string {
  return GENERIC_MESSAGES[code];
}

export function classifyStatus(status: number): ApiErrorCode {
  if (status === 401) return "UNAUTHORIZED";
  if (status === 403) return "FORBIDDEN";
  if (status === 404) return "NOT_FOUND";
  if (status === 409) return "CONFLICT";
  if (status === 422) return "VALIDATION_ERROR";
  if (status === 429) return "RATE_LIMITED";
  if (status >= 500) return "SERVER_ERROR";
  return "UNKNOWN";
}

/** Extracts a short, safe-to-show message from a FastAPI error body.
 * Never returns the raw body -- worst case, a generic fallback. */
export function extractDetailMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in (body as Record<string, unknown>)) {
    const detail = (body as Record<string, unknown>).detail;
    if (typeof detail === "string" && detail.length > 0 && detail.length < 300) {
      return detail;
    }
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0];
      if (first && typeof first === "object" && typeof (first as { msg?: unknown }).msg === "string") {
        return (first as { msg: string }).msg;
      }
    }
  }
  return fallback;
}
