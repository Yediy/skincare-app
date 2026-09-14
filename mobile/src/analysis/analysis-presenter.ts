import type { CaptureAssessment, MetricResult } from "@/types/domain";

/**
 * Pure presentation-mapping functions for analysis results (sections
 * 14/15/16/19). Nothing here computes, re-scores, or infers a new
 * value the backend didn't already produce -- every function is a
 * translation from a backend-owned code/number into safe, honest,
 * non-clinical copy. See ANALYSIS_ARCHITECTURE_MOBILE.md for the full
 * language policy this module implements.
 */

// backend/app/cv/capture_assessment.py's failure_reasons vocabulary --
// "{name}_too_low" / "{name}_marginal" for each sub-score, plus a
// fixed set of pose-specific codes. Anything not in this table still
// renders (never silently dropped), just with a generic fallback
// message rather than a fabricated specific one.
const CAPTURE_REASON_COPY: Record<string, string> = {
  blur_too_low: "The photo was too blurry.",
  blur_marginal: "The photo was slightly blurry.",
  exposure_too_low: "The lighting was too dark or too bright.",
  exposure_marginal: "The lighting was a little uneven.",
  lighting_balance_too_low: "The lighting was too uneven across your face.",
  lighting_balance_marginal: "The lighting was somewhat uneven.",
  face_size_too_low: "Your face was too small or too far from the camera.",
  face_size_marginal: "Try moving a little closer to the camera.",
  resolution_too_low: "The photo resolution was too low.",
  resolution_marginal: "The photo resolution was a little low.",
  occlusion_too_low: "Part of your face was covered or not visible.",
  occlusion_marginal: "Part of your face was partially covered.",
  head_pose_undetermined: "We couldn't determine your head position.",
  excessive_yaw: "Try facing the camera more directly (less turned to the side).",
  excessive_pitch: "Try facing the camera more directly (less tilted up or down).",
  excessive_roll: "Try holding your phone more level.",
  elevated_yaw: "Facing the camera a bit more directly may help.",
  elevated_pitch: "Leveling your head a bit more may help.",
  elevated_roll: "Holding your phone a bit more level may help.",
};

export function describeCaptureFailureReason(reason: string): string {
  return CAPTURE_REASON_COPY[reason] ?? "Image quality could be improved for this factor.";
}

export function describeCaptureFailureReasons(reasons: string[]): string[] {
  return reasons.map(describeCaptureFailureReason);
}

const QUALITY_STATUS_COPY: Record<CaptureAssessment["quality_status"], string> = {
  PASS: "Good capture quality",
  BORDERLINE: "Usable, but capture quality was limited",
  FAIL: "Capture quality was too low to analyze reliably",
};

export function describeCaptureQualityStatus(status: CaptureAssessment["quality_status"]): string {
  return QUALITY_STATUS_COPY[status];
}

const METRIC_STATUS_LABEL: Record<MetricResult["status"], string> = {
  VALID: "Measured",
  BORDERLINE: "Less certain",
  ABSTAINED: "Could not measure reliably",
};

export function describeMetricStatus(status: MetricResult["status"]): string {
  return METRIC_STATUS_LABEL[status];
}

/**
 * The single safety-critical rule of this module (section 15): an
 * ABSTAINED metric must never render a numeric value as though it
 * were a real measurement, no matter what `value` actually contains.
 * Every metric-rendering call site must go through this function
 * rather than reading `metric.value` directly.
 */
export function formatMetricValueForDisplay(metric: MetricResult): string | null {
  if (metric.status === "ABSTAINED") return null;
  if (metric.value === null) return null;
  return `${Math.round(metric.value * 100)}%`;
}

/** Prettifies a raw metric_name ("evenness_score") into label case
 * ("Evenness") -- purely cosmetic string formatting, not a
 * reinterpretation of what the backend measured. */
export function prettifyMetricName(metricName: string): string {
  const withoutScoreSuffix = metricName.replace(/_score$/, "");
  return withoutScoreSuffix
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export type AnalysisQualityLabel = "High" | "Moderate" | "Limited";

/**
 * Section 19: a compact, honest "Analysis quality: High/Moderate/
 * Limited" summary derived ONLY from real backend confidence/quality
 * signals -- never a separate, unrelated "AI confidence score."
 * Deliberately simple, documented thresholds (not a calibrated model
 * -- no such calibration data exists yet, same honesty this codebase
 * already applies to CV_VALIDATION_LIMITATIONS.md).
 */
export function deriveAnalysisQualityLabel(
  captureAssessment: CaptureAssessment,
  metricResults: MetricResult[],
): AnalysisQualityLabel {
  const measured = metricResults.filter((m) => m.status !== "ABSTAINED");
  const averageConfidence =
    measured.length > 0 ? measured.reduce((sum, m) => sum + m.confidence, 0) / measured.length : 0;

  if (captureAssessment.quality_status === "PASS" && averageConfidence >= 0.7) {
    return "High";
  }
  if (captureAssessment.quality_status === "FAIL" || averageConfidence < 0.4) {
    return "Limited";
  }
  return "Moderate";
}

// backend/app/api/v2/analyses.py's _SAFE_ERROR_CODES, mirrored for
// friendly copy -- the backend already collapses anything outside
// this set (plus its own "ANALYSIS_FAILED" fallback) before it ever
// reaches this client, so this table only needs to cover exactly that
// closed list.
const ERROR_CODE_COPY: Record<string, string> = {
  NO_FACE_DETECTED: "We couldn't detect a face in that photo. Please retake it facing the camera directly.",
  CAPTURE_QUALITY_FAILED: "The photo quality was too low to analyze. Please retake it with better lighting.",
  INVALID_IMAGE: "That photo couldn't be processed. Please retake it.",
  IMAGE_STORAGE_UNAVAILABLE: "We had trouble storing your photo. Please try again.",
  IMAGE_NOT_FOUND: "Your photo is no longer available. Please start a new analysis.",
  ENQUEUE_FAILED: "We had trouble starting your analysis. Please try again.",
  PROCESSING_FAILED: "Something went wrong while analyzing your photo. Please try again.",
  ANALYSIS_REQUEST_NOT_FOUND: "We couldn't find this analysis. Please start a new one.",
  INVALID_REQUEST_STATE: "This analysis is in an unexpected state. Please start a new one.",
  CONSENT_REQUIRED: "Consent is required before analysis can continue.",
  ANALYSIS_FAILED: "Something went wrong while analyzing your photo. Please try again.",
};

export function describeAnalysisErrorCode(errorCode: string | null | undefined): string {
  if (!errorCode) return ERROR_CODE_COPY.ANALYSIS_FAILED;
  return ERROR_CODE_COPY[errorCode] ?? ERROR_CODE_COPY.ANALYSIS_FAILED;
}

/** Whether a FAILED analysis's error is one a retake can plausibly
 * fix, vs. one where starting over from scratch is more appropriate. */
export function isRetakeableError(errorCode: string | null | undefined): boolean {
  return errorCode === "NO_FACE_DETECTED" || errorCode === "CAPTURE_QUALITY_FAILED" || errorCode === "INVALID_IMAGE";
}
