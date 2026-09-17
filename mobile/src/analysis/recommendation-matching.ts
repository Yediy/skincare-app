import type { Daypart, ProductRecommendation } from "@/types/domain";

/**
 * Pure routine-step <-> product-recommendation association (Mobile V1
 * Phase C1). The backend's own plan_step_key contract is
 * "AM:<step_number>" / "PM:<step_number>"
 * (app/domain/recommendation_service.py's StepProductRecommendation) --
 * this is the ONE place mobile builds or parses that key, so a routine
 * step is always matched to its recommendation by that explicit key,
 * never by array position (a client that instead zipped
 * plan.am_routine[i] with product_recommendations[i] would silently
 * mismatch the moment either list's ordering/length diverges, which is
 * exactly the kind of client-side recommendation invention this phase
 * must never do).
 */
export function buildPlanStepKey(daypart: Daypart, stepNumber: number): string {
  return `${daypart}:${stepNumber}`;
}

/**
 * Returns the one ProductRecommendation for this routine step, or
 * `null` when the backend didn't attach one (Phase 11's documented
 * fallback behavior -- an empty/missing match is valid, never an
 * error, and never something this app fills in with a client-chosen
 * substitute).
 *
 * More than one recommendation sharing the same plan_step_key should
 * be structurally impossible (the backend's own UNIQUE
 * (analysis_request_id, plan_step_key) constraint), but this function
 * fails deterministically -- returns `null` rather than guessing which
 * one is "the" real recommendation -- if it ever happens, since
 * picking one would itself be a client-side recommendation decision.
 */
export function getRecommendationForRoutineStep(
  daypart: Daypart,
  stepNumber: number,
  recommendations: ProductRecommendation[] | null | undefined,
): ProductRecommendation | null {
  if (!recommendations || recommendations.length === 0) return null;

  const key = buildPlanStepKey(daypart, stepNumber);
  const matches = recommendations.filter((rec) => rec.plan_step_key === key);

  if (matches.length !== 1) return null;
  return matches[0];
}
