/** Mirrors backend/app/main.py response shapes exactly -- this app
 * never invents fields the backend doesn't actually return. */

export type ExperienceLevel = "beginner" | "intermediate" | "advanced";

export type Profile = {
  has_sensitive_skin: boolean;
  experience_level: ExperienceLevel;
  max_routine_steps: number;
  is_pregnant: boolean;
  is_nursing: boolean;
  allergies: string[];
  avoid_ingredients: string[];
  skin_goals: string[];
  /** True only once the user has ever explicitly saved a profile --
   * distinguishes "never onboarded" from "chose every default." See
   * backend/app/db/profile_repository.py. */
  profile_set: boolean;
};

export type ProfileUpdateInput = Omit<Profile, "profile_set">;

export type ConsentStatus = {
  consent_type: string;
  required_policy_version: string;
  has_valid_consent: boolean;
};

export type AuthTokens = {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
};

export type MeResponse = {
  user_id: string;
};

// --- Analysis (Mobile V1 Phase B) -- mirrors backend/app/api/v2/analyses.py
// and backend/app/cv/capture_assessment.py/app/cv/metric_result.py exactly.

/** The backend's own closed status set (analysis_requests.status CHECK
 * constraint, backend/app/db/analysis_repository.py). RECEIVED and
 * QUEUED are both pre-processing and treated identically by the
 * mobile polling policy -- see src/analysis/analysis-lifecycle.ts. */
export type AnalysisRequestStatus = "RECEIVED" | "QUEUED" | "PROCESSING" | "COMPLETED" | "FAILED" | "CANCELLED";

export type CaptureQualityStatus = "PASS" | "BORDERLINE" | "FAIL";

/** Mirrors CaptureAssessment.to_dict() (backend/app/cv/capture_assessment.py)
 * exactly -- every sub-score the backend actually computes, nothing
 * mobile invents on its own. */
export type CaptureAssessment = {
  quality_status: CaptureQualityStatus;
  overall_quality: number;
  yaw: number;
  pitch: number;
  roll: number;
  blur_score: number;
  exposure_score: number;
  lighting_balance: number;
  face_size_score: number;
  resolution_score: number;
  occlusion_score: number;
  failure_reasons: string[];
  capture_pipeline_version?: string;
};

export type MetricStatus = "VALID" | "BORDERLINE" | "ABSTAINED";

/** One row of analysis_measurements, as returned by GET
 * /api/v2/analyses/{id} (see app/db/analysis_repository.py::get_measurements).
 * `value` is genuinely `null` for an ABSTAINED metric -- never a
 * fabricated number standing in for "we didn't measure this." */
export type MetricResult = {
  metric_name: string;
  value: number | null;
  confidence: number;
  status: MetricStatus;
  uncertainty_reasons: string[];
  metric_version?: string | null;
  calibration_version?: string | null;
};

export type TopPriority = {
  id: string;
  label: string;
  description: string;
  pillar: string;
  severity: number;
  display_order: number;
  default_intensity: string;
  effective_intensity: string;
};

/** One step of plan.am_routine/pm_routine (backend/app/services/plan_service.py).
 * `product_recommendations` exists on the backend shape but Phase B
 * deliberately renders only the read-only step text -- see item 18 of
 * this pass's brief (no product cards yet, that's Phase C). */
export type RoutineStep = {
  step_number: number;
  product_category: string;
  action: string;
  why: string;
  priority: string;
};

/** Deliberately loose/partial -- this app renders only the fields it
 * actually has a designed UI for (top_priorities, am/pm routine,
 * disclaimers). Mobile never independently computes or rescoring any
 * of this; it is exactly what the backend returned. */
export type AnalysisPlan = {
  top_priorities: TopPriority[];
  am_routine: RoutineStep[];
  pm_routine: RoutineStep[];
  disclaimers?: string[];
};

/** analysis_results row shape (app/db/analysis_repository.py::get_result). */
export type AnalysisResultData = {
  capture_assessment: CaptureAssessment;
  scores: Record<string, number>;
  plan: AnalysisPlan;
  eligible_for_longitudinal_comparison: boolean;
  pipeline_version: string;
};

/** GET /api/v2/analyses/{id} response (app/api/v2/analyses.py::AnalysisStatusResponse). */
export type AnalysisStatusResponse = {
  analysis_id: string;
  request_id: string;
  status: AnalysisRequestStatus;
  error_code?: string | null;
  result?: AnalysisResultData | null;
  product_recommendations?: unknown[] | null;
  metric_results?: MetricResult[] | null;
};

/** POST /api/v2/analyses response (202). */
export type AnalysisSubmitResponse = {
  analysis_id: string;
  request_id: string;
  status: AnalysisRequestStatus;
};
