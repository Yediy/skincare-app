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

Product/usage foundation pass: quota reservation
(app/domain/entitlement.py's UsagePolicyService) is now part of this
same request lifecycle, for the same reason the pieces above are --
whichever code path eventually calls this function (HTTP today, a
queue worker later) must get the same idempotent-reservation
guarantee, not a version that only exists in the HTTP route handler.
Reservation happens after the consent check (a request rejected for
missing consent never touches quota at all) and before any CV compute
runs; a failure anywhere in the CV/scoring/plan sequence releases the
reservation rather than consuming it -- only a fully successful
analysis consumes the slot.

Production recommendation pass (Part I, Phase 11 -- the P0 closure):
after PlanService.generate_plan() produces its abstract, category-level
plan, this function now calls
app.domain.recommendation_service.apply_product_matching_and_routine_safety()
to resolve concrete catalog products for it, each evaluated through
SafetyEngine.evaluate_product_formulation() and cross-checked by
evaluate_routine_safety() -- the actual production path a concrete
product recommendation goes through, not merely an interface waiting
for a caller. See PRODUCT_RECOMMENDATION_PIPELINE.md.

Async-execution pass (Part V/VI): `compute_analysis()` below is the
quota-independent core this module and
app.domain.analysis_execution_service.AnalysisExecutionService both
call -- image bytes in, CV/scoring/plan/product-matching/routine-safety
out, no reservation, no consumption, no HTTP exceptions. `perform_analysis`
is now a thin wrapper: it owns the *synchronous* request's reservation
lifecycle (reserve before compute, consume/release after) around a
call to the same `compute_analysis()`. The async worker path owns a
*different* reservation lifecycle -- the one AnalysisSubmissionService
already created at submission time -- so it calls `compute_analysis()`
directly and never reserves a second slot for the same logical
request. See ASYNC_ANALYSIS_ARCHITECTURE.md.
"""
import base64
from dataclasses import dataclass, field
from typing import Any, Dict, List
from uuid import UUID

import asyncpg

from app.cv.pipeline import FacialAnalysisPipeline
from app.db.user_constraint_repository import get_unresolved_constraint_flags
from app.domain.entitlement import UsagePolicyService
from app.domain.product_matching_service import ProductMatchingService
from app.domain.recommendation_service import apply_product_matching_and_routine_safety
from app.domain.safety_engine import SafetyEngine
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
    # Caller-supplied idempotency key -- see app/db/usage_repository.py
    # for the full contract (one request_id maps to one reservation
    # forever). Required, not optional: a caller with no real
    # idempotency key of its own must still supply *something*
    # unique per attempt (the HTTP layer generates a fresh UUID when
    # the client doesn't provide one) -- there is no "skip
    # idempotency" mode, since every request must still go through
    # exactly one reservation.
    request_id: str


@dataclass(frozen=True)
class AnalysisResult:
    plan: Dict[str, Any]
    scores: Dict[str, Any]
    metric_results: Dict[str, Any]
    capture_assessment: Dict[str, Any]
    eligible_for_longitudinal_comparison: bool
    # Provenance for every concrete product recommendation the routine
    # actually carries -- the exact shape
    # app.db.analysis_repository.commit_analysis_result() needs for its
    # analysis_product_recommendations rows (Part VI). Already fully
    # embedded, per-step, inside `plan` too (that's what /analyze's
    # JSON response reads) -- this is the same data, flattened, for a
    # caller (AnalysisExecutionService) that needs to persist it as
    # its own rows rather than read it back out of `plan`.
    product_recommendations: List[Dict[str, Any]] = field(default_factory=list)


async def compute_analysis(
    pool: asyncpg.Pool,
    user_id: UUID,
    image_bytes: bytes,
    *,
    pipeline: FacialAnalysisPipeline,
    scorer: FacialScorer,
    plan_service: PlanService,
    product_matching_service: ProductMatchingService,
    safety_engine: SafetyEngine,
) -> AnalysisResult:
    """The quota-independent core of a facial analysis: raw image bytes
    in, CV -> scoring -> abstract plan -> product matching -> routine
    safety out. No reservation, no consumption, no HTTP exceptions, no
    consent check -- every one of those is a *caller* concern (a
    synchronous request's reservation lifecycle in `perform_analysis`
    below, or the async worker's, which reuses a reservation
    AnalysisSubmissionService already created). Never call this twice
    for the same logical request without knowing which caller owns
    that request's quota slot.

    NoFaceDetectedError / CaptureQualityFailedError / ValueError from
    `pipeline.analyze()` propagate unchanged -- all three were already
    framework-agnostic exceptions before this module existed."""
    from app.db.profile_repository import get_profile

    extraction_result = pipeline.analyze(image_bytes)

    metric_results = extraction_result["metric_results"]
    capture_assessment = extraction_result["capture_assessment"]
    eligible_for_longitudinal_comparison = extraction_result["eligible_for_longitudinal_comparison"]

    # user_id is real, from a verified access token (sync path) or an
    # already-durable analysis_requests.user_id (async path) by the
    # time this is called. The rest is a real persisted profile
    # (app/db/profile_repository.py) -- falling back to the documented
    # DEFAULT_PROFILE only if the user has never set one.
    profile = await get_profile(pool, user_id)
    user_profile = {"user_id": str(user_id), **profile}

    analysis = scorer.compute_scores(metric_results, capture_quality=capture_assessment.overall_quality)
    plan = plan_service.generate_plan(
        analysis["scores"], analysis["insights"], user_profile, capture_assessment.overall_quality
    )

    # Part I, Phase 11 (the P0 closure): resolve the abstract plan's
    # categories to real catalog products, each independently
    # evaluated through SafetyEngine.evaluate_product_formulation()
    # and routine-level safety -- never produced from category-level
    # evaluate_offer() alone. has_unresolved_*_constraint flags come
    # from the normalized table (app/db/user_constraint_repository.py),
    # the actual source of truth for per-ingredient resolution;
    # user_profile's own allergies/avoid_ingredients/is_pregnant/
    # is_nursing/has_sensitive_skin already match the exact shape
    # SafetyEngine's constraints dict expects, so it's reused
    # directly rather than rebuilt.
    unresolved_flags = await get_unresolved_constraint_flags(pool, user_id)
    recommendation_constraints = {**user_profile, **unresolved_flags}
    recommendation_result = await apply_product_matching_and_routine_safety(
        pool, plan, recommendation_constraints,
        product_matching_service=product_matching_service,
        safety_engine=safety_engine,
    )

    return AnalysisResult(
        plan=plan,
        scores=analysis["scores"],
        metric_results=analysis["metric_results"],
        capture_assessment=capture_assessment.to_dict(),
        eligible_for_longitudinal_comparison=eligible_for_longitudinal_comparison,
        product_recommendations=[rec.to_dict() for rec in recommendation_result.product_recommendations],
    )


async def perform_analysis(
    pool: asyncpg.Pool,
    request: AnalysisRequest,
    *,
    pipeline: FacialAnalysisPipeline,
    scorer: FacialScorer,
    plan_service: PlanService,
    usage_policy_service: UsagePolicyService,
    product_matching_service: ProductMatchingService,
    safety_engine: SafetyEngine,
) -> AnalysisResult:
    from app.db.consent_repository import REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION, has_valid_consent

    user_uuid = UUID(request.user_id)

    if not await has_valid_consent(pool, user_uuid, REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION):
        raise ConsentRequiredError(REQUIRED_POLICY_VERSION)

    # QuotaExceededError (app.domain.entitlement) propagates uncaught
    # from here -- a request that never reserved a slot has nothing to
    # release. A replayed reservation (idempotent retry of a
    # request_id already RESERVED/CONSUMED/RELEASED earlier) returns
    # that same reservation rather than a fresh one; letting the
    # analysis proceed again for a replay of an already-CONSUMED
    # request_id is deliberate -- the caller asked for the same
    # logical request's *result*, not a second billable unit of it.
    #
    # This is the SYNCHRONOUS path's own reservation, owned start to
    # finish by this function -- distinct from the async path, where
    # AnalysisExecutionService reuses a reservation
    # AnalysisSubmissionService already created and never calls
    # reserve_analysis() itself. See compute_analysis()'s docstring.
    reservation = await usage_policy_service.reserve_analysis(user_uuid, request.request_id)

    try:
        try:
            image_bytes = base64.b64decode(request.image_base64, validate=True)
        except Exception as e:
            raise InvalidImageError("image_base64 is not valid base64") from e

        result = await compute_analysis(
            pool, user_uuid, image_bytes,
            pipeline=pipeline, scorer=scorer, plan_service=plan_service,
            product_matching_service=product_matching_service, safety_engine=safety_engine,
        )
    except Exception:
        # A legitimate failure anywhere past the reservation (bad
        # image, no face detected, capture quality too low, or any
        # unexpected error) releases the quota slot instead of
        # consuming it -- a failed attempt produced no usable result,
        # so it should not count against the user's allowance.
        # release() only touches rows still in RESERVED status, so
        # this is a safe no-op if the reservation was itself a replay
        # of a request_id some other call already resolved.
        await usage_policy_service.release_reservation(user_uuid, reservation.id)
        raise

    await usage_policy_service.consume_reservation(user_uuid, reservation.id)

    return result
