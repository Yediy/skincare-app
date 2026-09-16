/**
 * Component/integration-level coverage for ProductRecommendationCard's
 * fail-closed presentation invariant (Mobile V1 Phase C1 independent
 * review, blocker 1). The presenter-only test
 * (product-recommendation-presenter.test.ts) proves
 * getSafetyStatusPresentation()/isKnownSafetyStatus() classify an
 * unexpected status correctly -- it does NOT prove the component
 * actually acts on that classification. This file renders the real
 * component tree and asserts on what a user (and an accessibility
 * tree) actually sees.
 */
import React from "react";
import { render } from "@testing-library/react-native";

import { NoProductMatchNotice, ProductRecommendationCard, UnconfirmedProductMatchNotice } from "@/components/product-recommendation-card";
import { ThemeProvider } from "@/theme/theme-provider";
import type { ProductRecommendation } from "@/types/domain";

// This suite's first render() pays a one-time cold-start cost (native
// module mocks + the test-renderer/jest-expo stack initializing) that
// can exceed Jest's default 5s per-test timeout in a cold CI
// container -- bumped for this file only, not globally.
jest.setTimeout(20000);

function makeRec(overrides: Partial<ProductRecommendation> = {}): ProductRecommendation {
  return {
    plan_step_key: "AM:1",
    product_id: "product-1",
    formulation_id: "formulation-1",
    brand: "Acme Labs",
    product_name: "Gentle Cleanser",
    safety_status: "SAFE",
    reason_codes: [],
    restrictions: {},
    rules_version: "1.0",
    verification_date: "2026-01-15T00:00:00+00:00",
    rank_position: 1,
    ...overrides,
  };
}

async function renderCard(recommendation: ProductRecommendation) {
  return render(<ProductRecommendationCard recommendation={recommendation} />, { wrapper: ThemeProvider });
}

describe("ProductRecommendationCard -- known statuses", () => {
  it("renders a normal recommended-match card for SAFE", async () => {
    const { getByText, queryByText } = await renderCard(makeRec({ safety_status: "SAFE" }));
    expect(getByText("Recommended match")).toBeTruthy();
    expect(getByText("Acme Labs")).toBeTruthy();
    expect(getByText("Gentle Cleanser")).toBeTruthy();
    expect(queryByText(/could not be confirmed/i)).toBeNull();
  });

  it("renders a normal card with visible restriction text for RESTRICTED", async () => {
    const { getByText, queryByText } = await renderCard(
      makeRec({
        safety_status: "RESTRICTED",
        restrictions: { maximum_weekly_frequency: 2 },
      }),
    );
    expect(getByText("Recommended match")).toBeTruthy();
    expect(getByText(/Compatibility:.*restriction/i)).toBeTruthy();
    expect(getByText(/Limit to about 2x per week/i)).toBeTruthy();
    expect(queryByText(/could not be confirmed/i)).toBeNull();
  });
});

describe("ProductRecommendationCard -- fail-closed for unknown/unsafe statuses", () => {
  it.each(["UNSAFE", "INSUFFICIENT_DATA", "SOME_FUTURE_STATUS_THIS_APP_HAS_NEVER_SEEN"])(
    "never presents %s as a recommended product",
    async (safetyStatus) => {
      const { queryByText, getByText, toJSON } = await renderCard(
        makeRec({
          safety_status: safetyStatus,
          brand: "Acme Labs",
          product_name: "Retinol Serum",
          verification_date: "2026-01-15T00:00:00+00:00",
          restrictions: { maximum_weekly_frequency: 2 },
          reason_codes: ["ALLERGY_CONFLICT"],
        }),
      );

      // Never labeled as a recommendation.
      expect(queryByText("Recommended match")).toBeNull();
      expect(queryByText(/^Recommended match:/)).toBeNull();

      // Never exposes the concrete product's brand/name.
      expect(queryByText("Acme Labs")).toBeNull();
      expect(queryByText("Retinol Serum")).toBeNull();

      // Never shows verification provenance.
      expect(queryByText(/Formulation verified/i)).toBeNull();

      // Never shows interpreted restrictions/reasons for an
      // unconfirmed status.
      expect(queryByText(/Limit to about 2x per week/i)).toBeNull();
      expect(queryByText(/allergy/i)).toBeNull();

      // Renders the required fail-closed notice instead, and never
      // crashes doing it.
      expect(getByText(/Specific product match unavailable/i)).toBeTruthy();
      expect(getByText(/could not be confirmed/i)).toBeTruthy();

      // No accessibility label anywhere in the tree claims this is a
      // recommended match.
      const serialized = JSON.stringify(toJSON());
      expect(serialized).not.toMatch(/Recommended match/i);
    },
  );

  it("renders UnconfirmedProductMatchNotice standalone with the exact required fail-closed copy", async () => {
    const { getByText } = await render(<UnconfirmedProductMatchNotice />, { wrapper: ThemeProvider });
    expect(
      getByText(
        "Specific product match unavailable. Compatibility for the product returned with this analysis could not be confirmed.",
      ),
    ).toBeTruthy();
  });
});

describe("NoProductMatchNotice -- distinct from the fail-closed notice", () => {
  it("still renders its own distinct copy when no match was returned at all", async () => {
    const { getByText } = await render(<NoProductMatchNotice />, { wrapper: ThemeProvider });
    expect(getByText("No specific product match is available for this step yet.")).toBeTruthy();
  });
});
