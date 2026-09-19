import { PURCHASES_ERROR_CODE } from "react-native-purchases";

/**
 * Presentation-safe purchase/restore error classification (Mobile C2
 * Part 18). A RevenueCat `PurchasesError` object may carry
 * transaction/customer metadata this app must never log wholesale --
 * every call site logs (through src/utils/logger.ts) only the bounded
 * category this function returns, never the raw error object.
 */
export type PurchaseErrorCategory =
  | "USER_CANCELLED"
  | "STORE_UNAVAILABLE"
  | "NETWORK_ERROR"
  | "CONFIGURATION_ERROR"
  | "GENERIC_FAILURE";

const STORE_UNAVAILABLE_CODES = new Set<string>([
  PURCHASES_ERROR_CODE.STORE_PROBLEM_ERROR,
  PURCHASES_ERROR_CODE.PRODUCT_NOT_AVAILABLE_FOR_PURCHASE_ERROR,
  PURCHASES_ERROR_CODE.PURCHASE_NOT_ALLOWED_ERROR,
  PURCHASES_ERROR_CODE.PAYMENT_PENDING_ERROR,
  PURCHASES_ERROR_CODE.OFFLINE_CONNECTION_ERROR,
]);

const CONFIGURATION_ERROR_CODES = new Set<string>([
  PURCHASES_ERROR_CODE.CONFIGURATION_ERROR,
  PURCHASES_ERROR_CODE.INVALID_CREDENTIALS_ERROR,
  PURCHASES_ERROR_CODE.INVALID_APP_USER_ID_ERROR,
]);

/** Duck-typed on `code` -- accepts a real RevenueCat PurchasesError,
 * or anything else (a plain Error, a thrown string) without ever
 * throwing itself; an unrecognized shape classifies as GENERIC_FAILURE
 * rather than crashing the caller's error-handling path. */
export function classifyPurchaseError(error: unknown): PurchaseErrorCategory {
  const code = (error as { code?: string } | null | undefined)?.code;

  if (code === PURCHASES_ERROR_CODE.PURCHASE_CANCELLED_ERROR) {
    return "USER_CANCELLED";
  }
  if (code === PURCHASES_ERROR_CODE.NETWORK_ERROR) {
    return "NETWORK_ERROR";
  }
  if (code && STORE_UNAVAILABLE_CODES.has(code)) {
    return "STORE_UNAVAILABLE";
  }
  if (code && CONFIGURATION_ERROR_CODES.has(code)) {
    return "CONFIGURATION_ERROR";
  }
  return "GENERIC_FAILURE";
}

/** Short, user-facing copy per category -- never the raw error
 * message (which may originate from the store/provider and isn't
 * meant for end users). */
export function purchaseErrorMessage(category: PurchaseErrorCategory): string {
  switch (category) {
    case "USER_CANCELLED":
      return "Purchase cancelled.";
    case "STORE_UNAVAILABLE":
      return "The store is currently unavailable. Please try again later.";
    case "NETWORK_ERROR":
      return "A network error occurred. Check your connection and try again.";
    case "CONFIGURATION_ERROR":
      return "Purchases aren't available right now. Please try again later.";
    case "GENERIC_FAILURE":
    default:
      return "Something went wrong with your purchase. Please try again.";
  }
}
