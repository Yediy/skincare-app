import { buildPlanStepKey, getRecommendationForRoutineStep } from "@/analysis/recommendation-matching";
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

describe("buildPlanStepKey", () => {
  it("builds the exact backend plan_step_key contract", () => {
    expect(buildPlanStepKey("AM", 1)).toBe("AM:1");
    expect(buildPlanStepKey("PM", 3)).toBe("PM:3");
  });
});

describe("getRecommendationForRoutineStep", () => {
  it("maps an AM step to its recommendation by AM:<number>", () => {
    const recs = [makeRec({ plan_step_key: "AM:2", product_name: "AM Step 2 Product" })];
    expect(getRecommendationForRoutineStep("AM", 2, recs)?.product_name).toBe("AM Step 2 Product");
  });

  it("maps a PM step to its recommendation by PM:<number>", () => {
    const recs = [makeRec({ plan_step_key: "PM:3", product_name: "PM Step 3 Product" })];
    expect(getRecommendationForRoutineStep("PM", 3, recs)?.product_name).toBe("PM Step 3 Product");
  });

  it("never matches an AM step to a PM recommendation with the same step number", () => {
    const recs = [makeRec({ plan_step_key: "PM:1" })];
    expect(getRecommendationForRoutineStep("AM", 1, recs)).toBeNull();
  });

  it("returns null when no recommendation exists for this step (a valid, non-error state)", () => {
    expect(getRecommendationForRoutineStep("AM", 1, [])).toBeNull();
    expect(getRecommendationForRoutineStep("AM", 1, null)).toBeNull();
    expect(getRecommendationForRoutineStep("AM", 1, undefined)).toBeNull();
  });

  it("never associates by array position -- a recommendation at index 0 for a different step is ignored", () => {
    const recs = [makeRec({ plan_step_key: "AM:5" })];
    expect(getRecommendationForRoutineStep("AM", 1, recs)).toBeNull();
  });

  it("fails deterministically (returns null) rather than guessing when a plan_step_key is unexpectedly duplicated", () => {
    const recs = [
      makeRec({ plan_step_key: "AM:1", product_name: "First" }),
      makeRec({ plan_step_key: "AM:1", product_name: "Second" }),
    ];
    expect(getRecommendationForRoutineStep("AM", 1, recs)).toBeNull();
  });
});
