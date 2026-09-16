/**
 * Presentation-only layer for a ProductRecommendation (Mobile V1 Phase
 * C1). Every function here is pure and side-effect-free: it turns
 * backend-owned data into safe display copy, never a recommendation
 * or safety decision of its own. See ProductRecommendationCard, the
 * one place these are actually called from.
 *
 * Language discipline (per this phase's own requirement): never
 * "clinically proven"/"medically approved"/"guaranteed"/"best"/
 * "optimal"/"dermatologist recommended" anywhere in this file --
 * "product match"/"compatible match"/"recommended for this routine"/
 * "safety status"/"restrictions" only.
 */
import type { ProductRecommendation } from "@/types/domain";

/** Known, machine-readable reason codes this app can currently
 * translate into human copy (app/domain/safety_engine.py's own
 * reason-code vocabulary). Anything not listed here degrades safely
 * via describeReasonCode()'s fallback -- this app never invents a
 * medical interpretation for a code it doesn't recognize. */
const REASON_CODE_DESCRIPTIONS: Record<string, string> = {
  PREGNANCY_RESTRICTION: "This ingredient has a pregnancy-related usage restriction.",
  NURSING_RESTRICTION: "This ingredient has a nursing-related usage restriction.",
  SENSITIVE_SKIN_INTENSITY_LIMIT: "Usage frequency is limited for sensitive skin.",
  ACTIVE_INTERACTION_CONFLICT: "May interact with another ingredient in your routine.",
  MAX_FREQUENCY_EXCEEDED: "Usage frequency in your routine exceeds the recommended limit.",
  ALLERGY_CONFLICT: "Contains an ingredient matching one of your listed allergies.",
  USER_AVOID_INGREDIENT: "Contains an ingredient you've asked to avoid.",
};

/** Exact required fallback copy for a reason code this app doesn't
 * have a mapping for -- never a raw machine code, never a fabricated
 * medical interpretation. */
export const UNKNOWN_REASON_CODE_FALLBACK = "Additional compatibility considerations apply.";

export function describeReasonCode(code: string): string {
  return REASON_CODE_DESCRIPTIONS[code] ?? UNKNOWN_REASON_CODE_FALLBACK;
}

export function describeReasonCodes(codes: string[] | null | undefined): string[] {
  if (!codes || codes.length === 0) return [];
  return codes.map(describeReasonCode);
}

export type SafetyStatusTone = "neutral" | "caution" | "unknown";

export type SafetyStatusPresentation = {
  label: string;
  tone: SafetyStatusTone;
};

/**
 * SAFE/RESTRICTED are the only statuses the backend invariant permits
 * to reach a concrete recommendation. The `default` branch is a
 * deliberate fail-closed fallback for a status this app doesn't
 * recognize -- it never presents an unexpected value as a normal
 * recommended product, and it never crashes.
 */
export function getSafetyStatusPresentation(safetyStatus: string): SafetyStatusPresentation {
  switch (safetyStatus) {
    case "SAFE":
      return { label: "Compatible match", tone: "neutral" };
    case "RESTRICTED":
      return { label: "Compatible match, with usage restrictions", tone: "caution" };
    default:
      return { label: "Compatibility could not be confirmed for this item", tone: "unknown" };
  }
}

/** True only for the two statuses the backend contract documents.
 * ProductRecommendationCard uses this to decide whether to render
 * restrictions/reason-code detail at all -- an unknown status renders
 * only the generic fail-closed copy above, never restriction detail
 * that might imply a decision this app didn't actually verify. */
export function isKnownSafetyStatus(safetyStatus: string): safetyStatus is "SAFE" | "RESTRICTED" {
  return safetyStatus === "SAFE" || safetyStatus === "RESTRICTED";
}

/** Brand + product name, falling back gracefully when either or both
 * are absent (older analyses with no historical snapshot and no
 * resolvable current-catalog row -- see
 * app/db/analysis_repository.py::get_product_recommendations()). Never
 * invents a name; a fully-unresolved product says so plainly. */
export function getProductDisplayLabel(
  brand: string | null | undefined,
  productName: string | null | undefined,
): string {
  const parts = [brand, productName].filter(
    (part): part is string => typeof part === "string" && part.trim().length > 0,
  );
  if (parts.length === 0) return "Product details unavailable";
  return parts.join(" — ");
}

/**
 * "Formulation verified: <date>" provenance copy, or `null` when no
 * verification date is available (never rendered as "unverified" --
 * this phase's own requirement: the backend contract doesn't classify
 * absence that way, so this app must not invent that label). Never
 * implies clinical validation or medical certification -- verification
 * means only that this system's own catalog process recorded a
 * verification date for the formulation.
 */
export function formatVerificationProvenance(verificationDate: string | null | undefined): string | null {
  if (!verificationDate) return null;
  const parsed = new Date(verificationDate);
  if (Number.isNaN(parsed.getTime())) return null;
  const formatted = parsed.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
  return `Formulation verified: ${formatted}`;
}

/**
 * Restrictions -> a short list of plain-language considerations.
 * `restrictions` is a free-form JSON object
 * (app/domain/safety_engine.py's evaluate_product_formulation()) --
 * this function only recognizes the specific shapes that module
 * actually produces today and silently ignores anything else, rather
 * than dumping raw keys/values onto the user or guessing at meaning.
 */
export function summarizeRestrictions(restrictions: Record<string, unknown> | null | undefined): string[] {
  if (!restrictions || typeof restrictions !== "object") return [];
  const lines: string[] = [];

  if (typeof restrictions.maximum_weekly_frequency === "number") {
    lines.push(`Limit to about ${restrictions.maximum_weekly_frequency}x per week.`);
  }
  if (Array.isArray(restrictions.ingredient_frequency_caps) && restrictions.ingredient_frequency_caps.length > 0) {
    lines.push("Contains an ingredient with its own weekly usage limit.");
  }
  if (Array.isArray(restrictions.barrier_recovery_rules) && restrictions.barrier_recovery_rules.length > 0) {
    lines.push("May need to be paused during skin barrier recovery.");
  }
  if (Array.isArray(restrictions.advisory_rule_types) && restrictions.advisory_rule_types.length > 0) {
    lines.push("Additional usage guidance applies (for example, sun sensitivity or irritation potential).");
  }
  if (Array.isArray(restrictions.interactions) && restrictions.interactions.length > 0) {
    lines.push("May interact with another ingredient in your routine.");
  }

  return lines;
}

/** Everything ProductRecommendationCard needs, precomputed once so the
 * component itself stays purely presentational. */
export type ProductRecommendationPresentation = {
  displayLabel: string;
  brand: string | null;
  productName: string | null;
  safetyStatus: SafetyStatusPresentation;
  isKnownStatus: boolean;
  considerations: string[];
  verificationProvenance: string | null;
};

export function presentProductRecommendation(rec: ProductRecommendation): ProductRecommendationPresentation {
  const isKnownStatus = isKnownSafetyStatus(rec.safety_status);
  return {
    displayLabel: getProductDisplayLabel(rec.brand, rec.product_name),
    brand: rec.brand ?? null,
    productName: rec.product_name ?? null,
    safetyStatus: getSafetyStatusPresentation(rec.safety_status),
    isKnownStatus,
    // An unknown/unexpected safety status never surfaces restriction
    // detail -- only the generic fail-closed label above.
    considerations: isKnownStatus
      ? [...describeReasonCodes(rec.reason_codes), ...summarizeRestrictions(rec.restrictions)]
      : [],
    verificationProvenance: isKnownStatus ? formatVerificationProvenance(rec.verification_date) : null,
  };
}
