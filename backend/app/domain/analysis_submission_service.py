"""AnalysisSubmissionService (Part V, Phase 23-24 of the production-
recommendation/async-analysis pass): the async counterpart to
app.domain.analysis_service.perform_analysis, but this one does not
run the CV pipeline itself -- it verifies consent, reserves quota,
creates/reuses the durable analysis_requests row, stores the raw image
in ephemeral object storage, and enqueues a job for a worker to pick
up. See ASYNC_ANALYSIS_ARCHITECTURE.md for the full sequence.

Deliberately gated by settings.async_image_storage_enabled (default
False): see CONSENT_ASYNC_PROCESSING_REVIEW.md for why. This service
refuses to enqueue at all while the flag is off, rather than silently
falling back to synchronous behavior -- a caller wanting synchronous
analysis today uses POST /analyze (app.domain.analysis_service)
directly, which this module does not touch or depend on.

mark_queued() and JobQueue.enqueue() are composed inside one explicit
transaction (Phase 24's "atomic DB enqueue") so a request can never be
left QUEUED with no corresponding job, or vice versa.
"""
import base64
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import asyncpg

from app.db import analysis_repository
from app.domain.entitlement import UsagePolicyService
from app.queue.base import JobQueue
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore

ANALYSIS_JOB_TYPE = "analysis"


class AsyncImageStorageDisabledError(Exception):
    """Raised when settings.async_image_storage_enabled is False. The
    v2 HTTP layer maps this to 503 -- a deliberate, honest "this
    capability is not turned on yet", never a silent fallback to a
    different code path."""


class ConsentRequiredError(Exception):
    def __init__(self, required_policy_version: str):
        self.required_policy_version = required_policy_version
        super().__init__(f"Consent required for facial analysis (policy version {required_policy_version})")


class InvalidImageError(Exception):
    """Raised for a base64 payload that isn't valid base64 at all --
    mirrors app.domain.analysis_service.InvalidImageError exactly, kept
    as a separate class so this module has no import-time dependency on
    the synchronous path."""


@dataclass(frozen=True)
class SubmissionResult:
    analysis_request_id: UUID
    status: str
    # True if this call returned an already-existing analysis_request
    # for this exact request_id (Phase 23's idempotent-submit
    # requirement) rather than creating a fresh one -- no new
    # reservation, image, or job was created on a replay.
    replay: bool


class AnalysisSubmissionService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        usage_policy_service: UsagePolicyService,
        image_store: EphemeralAnalysisImageStore,
        job_queue: JobQueue,
        async_image_storage_enabled: bool,
    ):
        self._pool = pool
        self._usage_policy_service = usage_policy_service
        self._image_store = image_store
        self._job_queue = job_queue
        self._async_image_storage_enabled = async_image_storage_enabled

    async def submit(
        self,
        user_id: UUID,
        request_id: str,
        image_base64: str,
        *,
        region: str = "global",
    ) -> SubmissionResult:
        if not self._async_image_storage_enabled:
            raise AsyncImageStorageDisabledError()

        from app.db.consent_repository import REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION, has_valid_consent

        if not await has_valid_consent(self._pool, user_id, REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION):
            raise ConsentRequiredError(REQUIRED_POLICY_VERSION)

        # Idempotent submit (Phase 23): a retried POST with a
        # request_id that already has a durable analysis_requests row
        # returns that row's current state unchanged -- no second
        # reservation, image upload, or job.
        existing = await analysis_repository.get_request_by_request_id(self._pool, user_id, request_id)
        if existing is not None:
            return SubmissionResult(analysis_request_id=existing["id"], status=existing["status"], replay=True)

        # QuotaExceededError / AnalysisAlreadyCompletedError /
        # AnalysisInProgressError (app.domain.entitlement) propagate
        # uncaught -- same contract as perform_analysis's synchronous
        # path, mapped to HTTP by the v2 route.
        reservation = await self._usage_policy_service.reserve_analysis(user_id, request_id)

        try:
            image_bytes = base64.b64decode(image_base64)
        except Exception as e:
            await self._usage_policy_service.release_reservation(user_id, reservation.id)
            raise InvalidImageError("image_base64 is not valid base64") from e

        req = await analysis_repository.create_request(
            self._pool, user_id, request_id, usage_reservation_id=reservation.id, home_region=region,
        )

        try:
            stored = await self._image_store.store(image_bytes, region=region)
        except Exception:
            # Storage is down -- nothing durable to retry from yet
            # (Phase 23's own submission attempt failed outright), so
            # release the slot and mark the request FAILED rather than
            # leaving it stuck in RECEIVED forever.
            await self._usage_policy_service.release_reservation(user_id, reservation.id)
            await analysis_repository.mark_failed(self._pool, user_id, req["id"], "IMAGE_STORAGE_UNAVAILABLE")
            raise

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await analysis_repository.mark_queued(
                    self._pool, user_id, req["id"],
                    image_object_key=stored.object_key, image_expires_at=stored.expires_at,
                    conn=conn,
                )
                await self._job_queue.enqueue(
                    ANALYSIS_JOB_TYPE,
                    {"analysis_request_id": str(req["id"]), "user_id": str(user_id)},
                    request_id=request_id,
                    conn=conn,
                )

        return SubmissionResult(analysis_request_id=req["id"], status="QUEUED", replay=False)
