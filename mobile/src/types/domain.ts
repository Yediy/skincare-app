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

/** One step of plan.am_routine/pm_routine (backend/app/services/plan_service.py). */
export type RoutineStep = {
  step_number: number;
  product_category: string;
  action: string;
  why: string;
  priority: string;
};

/** "AM" or "PM" -- the daypart half of a plan_step_key ("AM:2"). */
export type Daypart = "AM" | "PM";

/** Mobile V1 Phase C1: a concrete product match for one routine step,
 * exactly the client-facing projection app/api/v2/analyses.py's
 * ProductRecommendationOut returns (never `SELECT *` --
 * app/db/analysis_repository.py::get_product_recommendations()).
 * `brand`/`product_name` are nullable: neither historical snapshot nor
 * current-catalog fallback could always resolve them, and this app
 * must never invent a display name when both are absent (see
 * src/analysis/product-recommendation-presenter.ts).
 *
 * `safety_status` is deliberately typed `string`, not a `"SAFE" |
 * "RESTRICTED"` union: the backend invariant is that an UNSAFE
 * concrete recommendation can never reach this client, but this type
 * must not assume that invariant holds forever -- an unexpected value
 * here must be handled as "fail closed," not crash a strict union
 * check. See getSafetyStatusPresentation().
 *
 * `rank_position` is provenance only (compatibility ordering, per
 * ProductMatchingService's own explicit "commercial firewall"
 * docstring) -- never render it as "#1 product"/"best product"/"top
 * ranked product"; no clinical-efficacy ranking exists. */
export type ProductRecommendation = {
  plan_step_key: string;
  product_id: string;
  formulation_id: string;
  brand: string | null;
  product_name: string | null;
  safety_status: string;
  reason_codes: string[];
  restrictions: Record<string, unknown>;
  rules_version: string;
  verification_date: string | null;
  rank_position: number;
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
  product_recommendations?: ProductRecommendation[] | null;
  metric_results?: MetricResult[] | null;
};

/** POST /api/v2/analyses response (202). */
export type AnalysisSubmitResponse = {
  analysis_id: string;
  request_id: string;
  status: AnalysisRequestStatus;
};

/** GET /api/v2/analyses (Mobile C3 -- history) response item --
 * mirrors app/api/v2/analyses.py::AnalysisHistoryItemOut exactly. A
 * summary row only, never the full result/plan -- see
 * AnalysisStatusResponse for that. */
export type AnalysisHistoryItem = {
  analysis_id: string;
  request_id: string;
  status: AnalysisRequestStatus;
  error_code?: string | null;
  created_at: string;
  completed_at?: string | null;
};

/** GET /api/v2/analyses response. `next_cursor` is opaque -- echoed
 * back verbatim as `?cursor=...` to fetch the next page; null/absent
 * means there is no next page. */
export type AnalysisHistoryResponse = {
  items: AnalysisHistoryItem[];
  next_cursor?: string | null;
};
