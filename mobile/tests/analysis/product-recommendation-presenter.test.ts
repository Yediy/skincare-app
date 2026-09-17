import {
  describeReasonCode,
  describeReasonCodes,
  formatVerificationProvenance,
  getProductDisplayLabel,
  getSafetyStatusPresentation,
  isKnownSafetyStatus,
  presentProductRecommendation,
  summarizeRestrictions,
  UNKNOWN_REASON_CODE_FALLBACK,
} from "@/analysis/product-recommendation-presenter";
import type { ProductRecommendation } from "@/types/domain";

function makeRec(overrides: Partial<ProductRecommendation> = {}): ProductRecommendation {
  return {
    plan_step_key: "AM:1",
    product_id: "product-1",
    formulation_id: "formulation-1",
    brand: "Brand",
    product_name: "Product",
    safety_status: "SAFE",
    reason_codes: [],
    restrictions: {},
    rules_version: "1.0",
    verification_date: null,
    rank_position: 1,
    ...overrides,
  };
}

describe("getSafetyStatusPresentation", () => {
  it("renders calm neutral copy for SAFE", () => {
    const result = getSafetyStatusPresentation("SAFE");
    expect(result.label).toBe("Compatible match");
    expect(result.tone).toBe("neutral");
  });

  it("renders restrictions-visible copy for RESTRICTED", () => {
    const result = getSafetyStatusPresentation("RESTRICTED");
    expect(result.tone).toBe("caution");
    expect(result.label.toLowerCase()).toContain("restriction");
  });

  it("fails closed for an unexpected/unknown status -- never presents it as a normal match", () => {
    const result = getSafetyStatusPresentation("UNSAFE");
    expect(result.tone).toBe("unknown");
    expect(result.label).not.toMatch(/compatible match$/i);

    const garbage = getSafetyStatusPresentation("this-is-not-a-real-status");
    expect(garbage.tone).toBe("unknown");
  });

  it("never uses forbidden clinical/superlative language", () => {
    for (const status of ["SAFE", "RESTRICTED", "UNSAFE", "unknown"]) {
      const label = getSafetyStatusPresentation(status).label.toLowerCase();
      for (const forbidden of ["clinically proven", "medically approved", "guaranteed", "best", "optimal", "dermatologist recommended"]) {
        expect(label).not.toContain(forbidden);
      }
    }
  });
});

describe("isKnownSafetyStatus", () => {
  it("is true only for SAFE and RESTRICTED", () => {
    expect(isKnownSafetyStatus("SAFE")).toBe(true);
    expect(isKnownSafetyStatus("RESTRICTED")).toBe(true);
    expect(isKnownSafetyStatus("UNSAFE")).toBe(false);
    expect(isKnownSafetyStatus("")).toBe(false);
  });
});

describe("getProductDisplayLabel", () => {
  it("joins brand and product name when both are present", () => {
    expect(getProductDisplayLabel("Brand", "Product")).toBe("Brand — Product");
  });

  it("degrades gracefully to product name alone when brand is missing", () => {
    expect(getProductDisplayLabel(null, "Product")).toBe("Product");
  });

  it("degrades gracefully to brand alone when product name is missing", () => {
    expect(getProductDisplayLabel("Brand", null)).toBe("Brand");
  });

  it("never invents a name when both are absent", () => {
    expect(getProductDisplayLabel(null, null)).toBe("Product details unavailable");
    expect(getProductDisplayLabel(undefined, undefined)).toBe("Product details unavailable");
  });

  it("treats a blank string the same as absent", () => {
    expect(getProductDisplayLabel("  ", "  ")).toBe("Product details unavailable");
  });
});

describe("formatVerificationProvenance", () => {
  it("formats a present verification date as provenance copy", () => {
    const result = formatVerificationProvenance("2026-01-15T00:00:00+00:00");
    expect(result).toContain("Formulation verified:");
    expect(result).not.toMatch(/clinically validated|universally safe|medically certified/i);
  });

  it("omits the date entirely when absent, never rendering 'unverified'", () => {
    expect(formatVerificationProvenance(null)).toBeNull();
    expect(formatVerificationProvenance(undefined)).toBeNull();
  });

  it("returns null rather than throwing on a malformed date string", () => {
    expect(formatVerificationProvenance("not-a-date")).toBeNull();
  });
});

describe("describeReasonCode / describeReasonCodes", () => {
  it("translates a known machine reason code into human copy", () => {
    expect(describeReasonCode("PREGNANCY_RESTRICTION")).not.toBe("PREGNANCY_RESTRICTION");
    expect(describeReasonCode("SENSITIVE_SKIN_INTENSITY_LIMIT").length).toBeGreaterThan(0);
  });

  it("degrades an unknown reason code to the exact required fallback copy, never the raw code", () => {
    const result = describeReasonCode("SOME_FUTURE_CODE_THIS_APP_DOESNT_KNOW");
    expect(result).toBe(UNKNOWN_REASON_CODE_FALLBACK);
    expect(result).not.toContain("SOME_FUTURE_CODE");
  });

  it("never dumps a raw machine code when a mapping exists", () => {
    for (const code of ["PREGNANCY_RESTRICTION", "NURSING_RESTRICTION", "SENSITIVE_SKIN_INTENSITY_LIMIT", "ACTIVE_INTERACTION_CONFLICT"]) {
      expect(describeReasonCodes([code])[0]).not.toBe(code);
    }
  });

  it("returns an empty list for no reason codes", () => {
    expect(describeReasonCodes([])).toEqual([]);
    expect(describeReasonCodes(null)).toEqual([]);
    expect(describeReasonCodes(undefined)).toEqual([]);
  });
});

describe("summarizeRestrictions", () => {
  it("summarizes a known restriction shape into plain language", () => {
    const lines = summarizeRestrictions({ maximum_weekly_frequency: 2 });
    expect(lines.some((l) => l.includes("2"))).toBe(true);
  });

  it("never dumps raw JSON -- unrecognized keys are silently ignored, not rendered", () => {
    const lines = summarizeRestrictions({ some_future_unrecognized_key: { nested: true } });
    expect(lines).toEqual([]);
  });

  it("handles null/undefined/non-object input without throwing", () => {
    expect(summarizeRestrictions(null)).toEqual([]);
    expect(summarizeRestrictions(undefined)).toEqual([]);
  });
});

describe("presentProductRecommendation", () => {
  it("presents a SAFE recommendation with reason codes/restrictions surfaced", () => {
    const presentation = presentProductRecommendation(
      makeRec({ safety_status: "SAFE", reason_codes: [], restrictions: {} }),
    );
    expect(presentation.safetyStatus.tone).toBe("neutral");
    expect(presentation.isKnownStatus).toBe(true);
  });

  it("presents a RESTRICTED recommendation with restrictions visibly listed as text", () => {
    const presentation = presentProductRecommendation(
      makeRec({
        safety_status: "RESTRICTED",
        reason_codes: ["SENSITIVE_SKIN_INTENSITY_LIMIT"],
        restrictions: { maximum_weekly_frequency: 2 },
      }),
    );
    expect(presentation.safetyStatus.tone).toBe("caution");
    expect(presentation.considerations.length).toBeGreaterThan(0);
    expect(presentation.considerations.join(" ")).not.toContain("SENSITIVE_SKIN_INTENSITY_LIMIT");
  });

  it("fails closed on an unexpected safety status: no considerations/verification are surfaced", () => {
    const presentation = presentProductRecommendation(
      makeRec({
        safety_status: "UNSAFE",
        reason_codes: ["ALLERGY_CONFLICT"],
        restrictions: { maximum_weekly_frequency: 1 },
        verification_date: "2026-01-01T00:00:00+00:00",
      }),
    );
    expect(presentation.isKnownStatus).toBe(false);
    expect(presentation.considerations).toEqual([]);
    expect(presentation.verificationProvenance).toBeNull();
  });

  it("degrades gracefully when brand/product name are both missing", () => {
    const presentation = presentProductRecommendation(makeRec({ brand: null, product_name: null }));
    expect(presentation.displayLabel).toBe("Product details unavailable");
  });

  it("includes verification provenance when a verification date is present", () => {
    const presentation = presentProductRecommendation(
      makeRec({ verification_date: "2026-01-01T00:00:00+00:00" }),
    );
    expect(presentation.verificationProvenance).toContain("Formulation verified:");
  });

  it("omits verification provenance when the verification date is absent", () => {
    const presentation = presentProductRecommendation(makeRec({ verification_date: null }));
    expect(presentation.verificationProvenance).toBeNull();
  });
});
