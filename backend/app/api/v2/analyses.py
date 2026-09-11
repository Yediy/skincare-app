"""POST /api/v2/analyses, GET /api/v2/analyses/{analysis_id} -- Part V
continuation: the first production HTTP surface for
AnalysisSubmissionService. Everything below is a thin adapter, same
shape as /analyze in app/main.py: translate AnalysisSubmissionService's
plain domain exceptions into deliberate HTTP responses, never leak a
stack trace or internal exception text to the client.

See ASYNC_ANALYSIS_ARCHITECTURE.md for the full request lifecycle this
sits in front of.
"""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.db import analysis_repository
from app.db.connection import get_db_pool
from app.domain.analysis_submission_service import (
    AnalysisSubmissionService,
    AsyncImageStorageDisabledError,
    ConsentRequiredError,
    InvalidImageError,
)
from app.domain.entitlement import (
    AnalysisAlreadyCompletedError,
    AnalysisInProgressError,
    FreeTierEntitlementService,
    QuotaExceededError,
    UsagePolicyService,
)
from app.domain.user_placement_service import UserPlacementNotFoundError, UserPlacementService
from app.middleware.rate_limiter import ANALYSIS_POLICY, GENERAL_POLICY, rate_limit_by_user
from app.queue.postgres_queue import PostgresJobQueue
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore
from app.storage.r2 import CloudflareR2ObjectStorage

router = APIRouter(prefix="/api/v2/analyses", tags=["analyses"])

# Real, working free-tier policy -- same singleton rationale as
# app/main.py's own module-level entitlement_service (stateless, no
# pool, safe to share across requests).
_entitlement_service = FreeTierEntitlementService()


def _build_submission_service() -> AnalysisSubmissionService:
    """Constructed per-request (wraps the request-time db pool), same
    pattern every other route in this codebase uses for its
    pool-dependent services. Building CloudflareR2ObjectStorage here
    even when settings.async_image_storage_enabled is False is safe --
    boto3 client construction makes no network call (see
    app/storage/r2.py) -- so a disabled deployment never needs R2
    credentials configured at all; AnalysisSubmissionService.submit()
    itself is what actually refuses to proceed while the flag is off."""
    pool = get_db_pool()
    object_storage = CloudflareR2ObjectStorage(
        account_id=settings.r2_account_id or "",
        access_key_id=settings.r2_access_key_id or "",
        secret_access_key=settings.r2_secret_access_key or "",
        bucket=settings.r2_bucket or "",
        endpoint_url=settings.r2_endpoint,
    )
    return AnalysisSubmissionService(
        pool,
        usage_policy_service=UsagePolicyService(pool, _entitlement_service),
        image_store=EphemeralAnalysisImageStore(object_storage),
        job_queue=PostgresJobQueue(pool),
        async_image_storage_enabled=settings.async_image_storage_enabled,
    )


class AnalysisSubmitRequest(BaseModel):
    image_base64: str
    # Required, deliberately -- unlike v1's synchronous /analyze
    # (which manufactures a UUID when the client omits one), this is a
    # retry-oriented async endpoint: a server-generated key would
    # defeat the entire point, since a client that timed out waiting
    # for the response has no way to learn which value the server
    # chose in order to retry with it. See module docstring.
    request_id: str = Field(min_length=1, max_length=255)


class AnalysisSubmitResponse(BaseModel):
    analysis_id: str
    request_id: str
    status: str


@router.post("", status_code=202, response_model=AnalysisSubmitResponse)
async def submit_analysis(
    request: AnalysisSubmitRequest,
    user_id: str = Depends(rate_limit_by_user(ANALYSIS_POLICY)),
):
    service = _build_submission_service()
    pool = get_db_pool()
    user_uuid = uuid.UUID(user_id)

    try:
        placement = await UserPlacementService(pool).get_placement(user_uuid)
    except UserPlacementNotFoundError:
        # rate_limit_by_user/get_current_user already verified this
        # user_id's row exists moments ago -- reaching here means it
        # vanished in between (or a genuine upstream bug), not that
        # this user was never real. Fail closed rather than silently
        # falling back to launch defaults as if they had been durably
        # assigned to nobody's row.
        raise HTTPException(status_code=500, detail="Unable to resolve account placement.")

    try:
        result = await service.submit(
            user_uuid, request.request_id, request.image_base64,
            region=placement.home_region, cell_id=placement.cell_id,
        )
    except AsyncImageStorageDisabledError:
        raise HTTPException(status_code=503, detail="Async analysis submission is not enabled.")
    except ConsentRequiredError as e:
        raise HTTPException(
            status_code=403,
            detail=f"Consent required for facial analysis (policy version {e.required_policy_version}). "
                   f"Grant it via POST /consent before calling POST /api/v2/analyses.",
        )
    except QuotaExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except AnalysisAlreadyCompletedError:
        raise HTTPException(
            status_code=409,
            detail="This request_id already completed. Retry with a new request_id for a new analysis.",
        )
    except AnalysisInProgressError:
        raise HTTPException(
            status_code=409,
            detail="This request_id is already being processed. Wait for it to finish, or retry with a new request_id.",
        )
    except InvalidImageError:
        raise HTTPException(status_code=422, detail="image_base64 is not valid base64")

    return AnalysisSubmitResponse(
        analysis_id=str(result.analysis_request_id), request_id=request.request_id, status=result.status,
    )


class AnalysisStatusResponse(BaseModel):
    analysis_id: str
    request_id: str
    status: str
    error_code: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    product_recommendations: Optional[List[Dict[str, Any]]] = None


# Safe, client-facing error classifications only -- never the raw
# error_code column value (which can carry an internal detail like
# "IMAGE_STORAGE_UNAVAILABLE" or "ENQUEUE_FAILED") and never an
# exception message. See app/domain/analysis_worker.py's retry
# classification for where these codes are actually assigned.
_SAFE_ERROR_CODES = {
    "NO_FACE_DETECTED", "CAPTURE_QUALITY_FAILED", "INVALID_IMAGE",
    "IMAGE_STORAGE_UNAVAILABLE", "IMAGE_NOT_FOUND", "ENQUEUE_FAILED", "PROCESSING_FAILED",
    "ANALYSIS_REQUEST_NOT_FOUND", "INVALID_REQUEST_STATE", "CONSENT_REQUIRED",
}


@router.get("/{analysis_id}", response_model=AnalysisStatusResponse)
async def get_analysis(
    analysis_id: str,
    user_id: str = Depends(rate_limit_by_user(GENERAL_POLICY)),
):
    pool = get_db_pool()
    user_uuid = uuid.UUID(user_id)

    try:
        analysis_uuid = uuid.UUID(analysis_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Analysis not found")

    # get_request_by_id sets app.current_user_id to the *calling* user
    # before the SELECT -- RLS (migration b034483cb876's
    # analysis_requests_isolation policy) scopes the row to that user
    # regardless of `analysis_uuid`'s value, so another user's analysis
    # comes back as None here, identical to a genuinely nonexistent id.
    # This route must never distinguish "not yours" from "doesn't
    # exist" -- that would leak the existence of other users' analyses.
    req = await analysis_repository.get_request_by_id(pool, user_uuid, analysis_uuid)
    if req is None:
        raise HTTPException(status_code=404, detail="Analysis not found")

    response = AnalysisStatusResponse(
        analysis_id=str(req["id"]), request_id=req["request_id"], status=req["status"],
    )

    if req["status"] == "COMPLETED":
        response.result = await analysis_repository.get_result(pool, user_uuid, analysis_uuid)
        response.product_recommendations = await analysis_repository.get_product_recommendations(
            pool, user_uuid, analysis_uuid,
        )
    elif req["status"] == "FAILED":
        raw_code = req.get("error_code")
        response.error_code = raw_code if raw_code in _SAFE_ERROR_CODES else "ANALYSIS_FAILED"

    return response
