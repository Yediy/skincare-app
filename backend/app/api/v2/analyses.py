"""POST /api/v2/analyses, GET /api/v2/analyses/{analysis_id} -- Part V
continuation: the first production HTTP surface for
AnalysisSubmissionService. Everything below is a thin adapter, same
shape as /analyze in app/main.py: translate AnalysisSubmissionService's
plain domain exceptions into deliberate HTTP responses, never leak a
stack trace or internal exception text to the client.

See ASYNC_ANALYSIS_ARCHITECTURE.md for the full request lifecycle this
sits in front of.
"""
import base64
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.db import analysis_repository
from app.db.connection import get_db_pool
from app.domain.analysis_submission_service import (
    AnalysisSubmissionService,
    AsyncImageStorageDisabledError,
    ConsentRequiredError,
    ImagePayloadTooLargeError,
    InvalidImageError,
)
from app.domain.entitlement import (
    AnalysisAlreadyCompletedError,
    AnalysisInProgressError,
    QuotaExceededError,
    UsagePolicyService,
    build_entitlement_service,
)
from app.domain.user_placement_service import UserPlacementNotFoundError, UserPlacementService
from app.middleware.rate_limiter import ANALYSIS_POLICY, GENERAL_POLICY, rate_limit_by_user
from app.queue.postgres_queue import PostgresJobQueue
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore
from app.storage.r2 import CloudflareR2ObjectStorage

router = APIRouter(prefix="/api/v2/analyses", tags=["analyses"])


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
        usage_policy_service=UsagePolicyService(pool, build_entitlement_service(pool)),
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
    except ImagePayloadTooLargeError:
        # Section 7F: a deliberate, safe 413 -- never the underlying
        # byte-count numbers or any decoder detail.
        raise HTTPException(status_code=413, detail="Image payload is too large.")
    except InvalidImageError:
        raise HTTPException(status_code=422, detail="image_base64 is not a valid, supported image")

    return AnalysisSubmitResponse(
        analysis_id=str(result.analysis_request_id), request_id=request.request_id, status=result.status,
    )


class ProductRecommendationOut(BaseModel):
    """Mobile V1 Phase C1's client-facing product-recommendation
    contract. Deliberately excludes database bookkeeping (`id`,
    `user_id`, `analysis_request_id`, `created_at`) that
    get_product_recommendations() never even selects in the first
    place -- this model is a second, independent enforcement layer:
    even if that query were ever changed to `SELECT *` again by
    mistake, an unlisted field here is silently dropped, never
    forwarded to the client (Pydantic v2's default `extra='ignore'`).

    `rank_position` is kept only as provenance, never as a claim of
    demonstrated clinical efficacy -- see
    ProductMatchingService._compatibility_ordering_key's own docstring.
    Mobile must never render "#1 product"/"best product"/"top ranked
    product" from this field."""
    plan_step_key: str
    product_id: str
    formulation_id: str
    brand: Optional[str] = None
    product_name: Optional[str] = None
    safety_status: str
    reason_codes: List[str]
    restrictions: Dict[str, Any]
    rules_version: str
    verification_date: Optional[str] = None
    rank_position: int


class AnalysisStatusResponse(BaseModel):
    analysis_id: str
    request_id: str
    status: str
    error_code: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    product_recommendations: Optional[List[ProductRecommendationOut]] = None
    # Mobile V1 Phase B addition: per-metric VALID/BORDERLINE/ABSTAINED
    # detail (see app/db/analysis_repository.py::get_measurements) --
    # additive only, every existing field/behavior above is unchanged.
    metric_results: Optional[List[Dict[str, Any]]] = None


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
        # Explicit construction (not a raw dict assignment) is what
        # actually makes ProductRecommendationOut's field allowlist
        # real: Pydantic only validates/strips extra fields when a
        # model is constructed, never when a plain list of dicts is
        # assigned to an already-built response object's field.
        response.product_recommendations = [
            ProductRecommendationOut(**rec)
            for rec in await analysis_repository.get_product_recommendations(pool, user_uuid, analysis_uuid)
        ]
        response.metric_results = await analysis_repository.get_measurements(pool, user_uuid, analysis_uuid)
    elif req["status"] == "FAILED":
        raw_code = req.get("error_code")
        response.error_code = raw_code if raw_code in _SAFE_ERROR_CODES else "ANALYSIS_FAILED"

    return response


# Mobile C3 (history/progress) -- read-only list of the calling user's
# own past analyses. Deliberately never includes the full result/plan
# payload (that's what GET /{analysis_id} is for) -- a summary row
# only, so a large history stays cheap to page through.
_HISTORY_DEFAULT_LIMIT = 20
_HISTORY_MAX_LIMIT = 50


class AnalysisHistoryItemOut(BaseModel):
    analysis_id: str
    request_id: str
    status: str
    error_code: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None


class AnalysisHistoryResponse(BaseModel):
    items: List[AnalysisHistoryItemOut]
    # Opaque -- echo back verbatim as ?cursor=... to fetch the next
    # page. Absent/null means there is no next page.
    next_cursor: Optional[str] = None


def _encode_history_cursor(created_at: datetime, request_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{request_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_history_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(created_at_str), uuid.UUID(id_str)
    except (ValueError, UnicodeDecodeError):
        # Never leak WHY decoding failed -- a malformed/tampered
        # client-supplied cursor is indistinguishable from any other
        # malformed one.
        raise HTTPException(status_code=400, detail="Invalid cursor")


@router.get("", response_model=AnalysisHistoryResponse)
async def list_analyses(
    limit: int = Query(default=_HISTORY_DEFAULT_LIMIT, ge=1, le=_HISTORY_MAX_LIMIT),
    cursor: Optional[str] = None,
    user_id: str = Depends(rate_limit_by_user(GENERAL_POLICY)),
):
    pool = get_db_pool()
    user_uuid = uuid.UUID(user_id)

    before_created_at: Optional[datetime] = None
    before_id: Optional[uuid.UUID] = None
    if cursor:
        before_created_at, before_id = _decode_history_cursor(cursor)

    # Fetch one extra row to learn whether another page exists without
    # a separate COUNT(*) query.
    rows = await analysis_repository.list_requests_by_user(
        pool, user_uuid, limit=limit + 1, before_created_at=before_created_at, before_id=before_id,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]

    items = [
        AnalysisHistoryItemOut(
            analysis_id=str(row["id"]),
            request_id=row["request_id"],
            status=row["status"],
            error_code=(
                (row["error_code"] if row["error_code"] in _SAFE_ERROR_CODES else "ANALYSIS_FAILED")
                if row["error_code"]
                else None
            ),
            created_at=row["created_at"].isoformat(),
            completed_at=row["completed_at"].isoformat() if row["completed_at"] else None,
        )
        for row in rows
    ]
    next_cursor = _encode_history_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None

    return AnalysisHistoryResponse(items=items, next_cursor=next_cursor)
