import {
  deriveAnalysisQualityLabel,
  describeAnalysisErrorCode,
  describeAnalysisRequestStatus,
  describeCaptureFailureReason,
  describeMetricStatus,
  formatMetricValueForDisplay,
  isRetakeableError,
  prettifyMetricName,
} from "@/analysis/analysis-presenter";
import type { CaptureAssessment, MetricResult } from "@/types/domain";

const baseAssessment: CaptureAssessment = {
  quality_status: "PASS",
  overall_quality: 0.9,
  yaw: 2, pitch: 1, roll: 0,
  blur_score: 0.9, exposure_score: 0.9, lighting_balance: 0.9,
  face_size_score: 0.9, resolution_score: 0.9, occlusion_score: 0.9,
  failure_reasons: [],
};

describe("formatMetricValueForDisplay", () => {
  it("never renders a numeric value for an ABSTAINED metric, regardless of what value contains", () => {
    const abstained: MetricResult = {
      metric_name: "redness_score", value: 0.42, confidence: 0.1, status: "ABSTAINED", uncertainty_reasons: ["x"],
    };
    expect(formatMetricValueForDisplay(abstained)).toBeNull();
  });

  it("also returns null for an ABSTAINED metric whose value is genuinely null", () => {
    const abstained: MetricResult = {
      metric_name: "redness_score", value: null, confidence: 0.1, status: "ABSTAINED", uncertainty_reasons: [],
    };
    expect(formatMetricValueForDisplay(abstained)).toBeNull();
  });

  it("renders a VALID metric's value as a percentage", () => {
    const valid: MetricResult = {
      metric_name: "evenness_score", value: 0.72, confidence: 0.9, status: "VALID", uncertainty_reasons: [],
    };
    expect(formatMetricValueForDisplay(valid)).toBe("72%");
  });

  it("renders a BORDERLINE metric's value too, distinct from ABSTAINED's null", () => {
    const borderline: MetricResult = {
      metric_name: "oil_score", value: 0.55, confidence: 0.5, status: "BORDERLINE", uncertainty_reasons: [],
    };
    expect(formatMetricValueForDisplay(borderline)).toBe("55%");
  });

  it("returns null if a non-abstained metric somehow has a null value (defensive, never fabricates)", () => {
    const oddValid: MetricResult = {
      metric_name: "x", value: null, confidence: 0.9, status: "VALID", uncertainty_reasons: [],
    };
    expect(formatMetricValueForDisplay(oddValid)).toBeNull();
  });
});

describe("describeMetricStatus", () => {
  it("maps every status to distinct, non-clinical copy", () => {
    expect(describeMetricStatus("VALID")).toBe("Measured");
    expect(describeMetricStatus("BORDERLINE")).toBe("Less certain");
    expect(describeMetricStatus("ABSTAINED")).toBe("Could not measure reliably");
  });
});

describe("prettifyMetricName", () => {
  it("converts a snake_case metric_name into label case", () => {
    expect(prettifyMetricName("evenness_score")).toBe("Evenness");
    expect(prettifyMetricName("under_eye_shadow_score")).toBe("Under Eye Shadow");
  });
});

describe("describeCaptureFailureReason", () => {
  it("maps known backend reason codes to specific, actionable copy", () => {
    expect(describeCaptureFailureReason("blur_too_low")).toMatch(/blurry/i);
    expect(describeCaptureFailureReason("excessive_yaw")).toMatch(/facing the camera/i);
  });

  it("falls back to a generic message for an unrecognized code rather than fabricating specifics", () => {
    expect(describeCaptureFailureReason("some_future_unknown_reason")).toBe(
      "Image quality could be improved for this factor.",
    );
  });
});

describe("deriveAnalysisQualityLabel", () => {
  it("is High when capture passed and average confidence is strong", () => {
    const metrics: MetricResult[] = [
      { metric_name: "a", value: 0.8, confidence: 0.9, status: "VALID", uncertainty_reasons: [] },
      { metric_name: "b", value: 0.7, confidence: 0.8, status: "VALID", uncertainty_reasons: [] },
    ];
    expect(deriveAnalysisQualityLabel(baseAssessment, metrics)).toBe("High");
  });

  it("is Limited when capture quality failed outright", () => {
    const failedAssessment: CaptureAssessment = { ...baseAssessment, quality_status: "FAIL" };
    const metrics: MetricResult[] = [
      { metric_name: "a", value: 0.8, confidence: 0.9, status: "VALID", uncertainty_reasons: [] },
    ];
    expect(deriveAnalysisQualityLabel(failedAssessment, metrics)).toBe("Limited");
  });

  it("is Limited when every metric abstained (average confidence among measured metrics is zero)", () => {
    const metrics: MetricResult[] = [
      { metric_name: "a", value: null, confidence: 0.1, status: "ABSTAINED", uncertainty_reasons: ["x"] },
    ];
    expect(deriveAnalysisQualityLabel(baseAssessment, metrics)).toBe("Limited");
  });

  it("is Moderate for a borderline-ish middle case", () => {
    const borderlineAssessment: CaptureAssessment = { ...baseAssessment, quality_status: "BORDERLINE" };
    const metrics: MetricResult[] = [
      { metric_name: "a", value: 0.6, confidence: 0.6, status: "VALID", uncertainty_reasons: [] },
    ];
    expect(deriveAnalysisQualityLabel(borderlineAssessment, metrics)).toBe("Moderate");
  });
});

describe("describeAnalysisErrorCode", () => {
  it("maps every backend-safe error code to friendly copy", () => {
    expect(describeAnalysisErrorCode("NO_FACE_DETECTED")).toMatch(/face/i);
    expect(describeAnalysisErrorCode("CAPTURE_QUALITY_FAILED")).toMatch(/quality/i);
  });

  it("falls back to a generic message for null/undefined/unrecognized codes", () => {
    expect(describeAnalysisErrorCode(null)).toBe(describeAnalysisErrorCode("ANALYSIS_FAILED"));
    expect(describeAnalysisErrorCode(undefined)).toBe(describeAnalysisErrorCode("ANALYSIS_FAILED"));
    expect(describeAnalysisErrorCode("SOME_UNKNOWN_FUTURE_CODE")).toBe(describeAnalysisErrorCode("ANALYSIS_FAILED"));
  });
});

describe("isRetakeableError", () => {
  it("is true for capture-quality-shaped failures", () => {
    expect(isRetakeableError("NO_FACE_DETECTED")).toBe(true);
    expect(isRetakeableError("CAPTURE_QUALITY_FAILED")).toBe(true);
    expect(isRetakeableError("INVALID_IMAGE")).toBe(true);
  });

  it("is false for infrastructure/state failures a retake cannot fix", () => {
    expect(isRetakeableError("IMAGE_STORAGE_UNAVAILABLE")).toBe(false);
    expect(isRetakeableError("PROCESSING_FAILED")).toBe(false);
    expect(isRetakeableError(null)).toBe(false);
  });
});

describe("describeAnalysisRequestStatus", () => {
  it("maps every known analysis_requests.status value to a distinct label", () => {
    const known = ["RECEIVED", "QUEUED", "PROCESSING", "COMPLETED", "FAILED", "CANCELLED"];
    const labels = known.map(describeAnalysisRequestStatus);
    expect(new Set(labels).size).toBe(known.length);
    for (const label of labels) {
      expect(label).not.toBe("Unknown");
    }
  });

  it("falls back to a safe label for an unrecognized status rather than rendering the raw value", () => {
    expect(describeAnalysisRequestStatus("SOME_FUTURE_STATUS")).toBe("Unknown");
  });
});
