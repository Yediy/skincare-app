import { PURCHASES_ERROR_CODE } from "react-native-purchases";

import { classifyPurchaseError, purchaseErrorMessage } from "@/billing/purchase-error";

describe("classifyPurchaseError", () => {
  it("classifies user cancellation", () => {
    expect(classifyPurchaseError({ code: PURCHASES_ERROR_CODE.PURCHASE_CANCELLED_ERROR })).toBe("USER_CANCELLED");
  });

  it("classifies network errors", () => {
    expect(classifyPurchaseError({ code: PURCHASES_ERROR_CODE.NETWORK_ERROR })).toBe("NETWORK_ERROR");
  });

  it("classifies store-unavailable errors", () => {
    expect(classifyPurchaseError({ code: PURCHASES_ERROR_CODE.STORE_PROBLEM_ERROR })).toBe("STORE_UNAVAILABLE");
    expect(classifyPurchaseError({ code: PURCHASES_ERROR_CODE.PRODUCT_NOT_AVAILABLE_FOR_PURCHASE_ERROR })).toBe(
      "STORE_UNAVAILABLE",
    );
  });

  it("classifies configuration errors", () => {
    expect(classifyPurchaseError({ code: PURCHASES_ERROR_CODE.CONFIGURATION_ERROR })).toBe("CONFIGURATION_ERROR");
    expect(classifyPurchaseError({ code: PURCHASES_ERROR_CODE.INVALID_APP_USER_ID_ERROR })).toBe(
      "CONFIGURATION_ERROR",
    );
  });

  it("falls back to generic failure for an unrecognized code", () => {
    expect(classifyPurchaseError({ code: PURCHASES_ERROR_CODE.UNKNOWN_ERROR })).toBe("GENERIC_FAILURE");
  });

  it("never throws for a non-error-shaped value", () => {
    expect(classifyPurchaseError(null)).toBe("GENERIC_FAILURE");
    expect(classifyPurchaseError(undefined)).toBe("GENERIC_FAILURE");
    expect(classifyPurchaseError("a plain string")).toBe("GENERIC_FAILURE");
    expect(classifyPurchaseError(new Error("plain error, no code"))).toBe("GENERIC_FAILURE");
  });
});

describe("purchaseErrorMessage", () => {
  it("returns bounded, non-empty user-facing copy for every category", () => {
    const categories = [
      "USER_CANCELLED",
      "STORE_UNAVAILABLE",
      "NETWORK_ERROR",
      "CONFIGURATION_ERROR",
      "GENERIC_FAILURE",
    ] as const;
    for (const category of categories) {
      const message = purchaseErrorMessage(category);
      expect(typeof message).toBe("string");
      expect(message.length).toBeGreaterThan(0);
    }
  });
});
