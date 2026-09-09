"""Domain boundary for a full facial-analysis run (Phase 14 of the
platform-foundation brief).

Before this module existed, `/analyze` (app/main.py) inlined the whole
sequence -- consent check, base64 decode, CV pipeline, profile fetch,
scoring, plan generation -- directly in the HTTP route handler, mixing
`HTTPException`s into what is otherwise pure domain logic. Every piece
it calls (`FacialAnalysisPipeline`, `FacialScorer`, `PlanService`,
`SafetyEngine` underneath the plan service) was already synchronous
and HTTP-agnostic; the coupling was entirely in the orchestration, not
in the underlying work.

`perform_analysis` is that orchestration, extracted and made
HTTP-agnostic: it raises plain, framework-independent exceptions
(`ConsentRequiredError`, `InvalidImageError`, plus the pre-existing
`NoFaceDetectedError`/`CaptureQualityFailedError` from
`app.cv.pipeline`, left as-is since they were already like this) and
returns a plain `AnalysisResult`. `/analyze` is now a thin adapter that
calls this and translates the result/exceptions into an HTTP response.

This does not, by itself, move `/analyze` onto a queue -- it still
runs this synchronously inline on the request, same as before. What it
does is remove the one real obstacle to a future background worker
calling this exact function: `pipeline`/`scorer`/`plan_service` are
passed in (dependency injection), not constructed here, so a worker
process can construct its own singletons and call the same function
with no changes to this module.
"""
import base64
from dataclasses import dataclass
from typing import Any, Dict
from uuid import UUID

import asyncpg

from app.cv.pipeline import FacialAnalysisPipeline
from app.ml.scorer import FacialScorer
from app.services.plan_service import PlanService


class ConsentRequiredError(Exception):
    def __init__(self, required_policy_version: str):
        self.required_policy_version = required_policy_version
        super().__init__(f"Consent required for facial analysis (policy version {required_policy_version})")


class InvalidImageError(Exception):
    """Raised for a base64 payload that isn't valid base64 at all.
    Distinct from the plain `ValueError` `FacialAnalysisPipeline.analyze`
    itself raises for base64 that decodes fine but isn't a valid
    image -- that one is left to propagate as-is, same as before this
    module existed."""


@dataclass(frozen=True)
class AnalysisRequest:
    user_id: str
    image_base64: str


@dataclass(frozen=True)
class AnalysisResult:
    plan: Dict[str, Any]
    scores: Dict[str, Any]
    metric_results: Dict[str, Any]
    capture_assessment: Dict[str, Any]
    eligible_for_longitudinal_comparison: bool


async def perform_analysis(
    pool: asyncpg.Pool,
    request: AnalysisRequest,
    *,
    pipeline: FacialAnalysisPipeline,
    scorer: FacialScorer,
    plan_service: PlanService,
) -> AnalysisResult:
    from app.db.consent_repository import REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION, has_valid_consent
    from app.db.profile_repository import get_profile

    user_uuid = UUID(request.user_id)

    if not await has_valid_consent(pool, user_uuid, REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION):
        raise ConsentRequiredError(REQUIRED_POLICY_VERSION)

    try:
        image_bytes = base64.b64decode(request.image_base64)
    except Exception as e:
        raise InvalidImageError("image_base64 is not valid base64") from e

    # NoFaceDetectedError / CaptureQualityFailedError / ValueError from
    # here propagate unchanged -- all three were already
    # framework-agnostic exceptions before this module existed.
    extraction_result = pipeline.analyze(image_bytes)

    metric_results = extraction_result["metric_results"]
    capture_assessment = extraction_result["capture_assessment"]
    eligible_for_longitudinal_comparison = extraction_result["eligible_for_longitudinal_comparison"]

    # user_id is real, from a verified access token by the time this
    # is called. The rest is a real persisted profile
    # (app/db/profile_repository.py) -- falling back to the documented
    # DEFAULT_PROFILE only if the user has never set one.
    profile = await get_profile(pool, user_uuid)
    user_profile = {"user_id": request.user_id, **profile}

    analysis = scorer.compute_scores(metric_results, capture_quality=capture_assessment.overall_quality)
    plan = plan_service.generate_plan(
        analysis["scores"], analysis["insights"], user_profile, capture_assessment.overall_quality
    )

    return AnalysisResult(
        plan=plan,
        scores=analysis["scores"],
        metric_results=analysis["metric_results"],
        capture_assessment=capture_assessment.to_dict(),
        eligible_for_longitudinal_comparison=eligible_for_longitudinal_comparison,
    )
