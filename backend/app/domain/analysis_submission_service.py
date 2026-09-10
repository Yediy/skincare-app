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

Failure-saga hardening (this pass): two failure windows this module's
own happy path did not previously cover, plus one concurrency gap.

  - create_request() failing after reserve_analysis() succeeded would
    leave the reservation stranded RESERVED forever (nothing else ever
    releases it, since the caller never gets an analysis_request to
    key a later release off of). Now wrapped: any exception here
    releases the reservation before propagating.
  - The image is uploaded to ephemeral storage *before* the atomic
    mark_queued()+enqueue() transaction; if that transaction fails
    after a successful upload, the object would otherwise be orphaned
    (no DB reference for the cleanup sweeper to find, since mark_queued
    -- the only thing that would have written image_object_key/
    image_expires_at -- is exactly what didn't commit). Compensation:
    attempt immediate deletion; if that succeeds, nothing durable to
    clean up. If deletion itself fails (storage unavailable), the
    image reference is written directly (independent of the failed
    transaction) so find_overdue_ephemeral_images() -- the sweeper --
    can recover it once image_expires_at passes. Either way, the
    reservation is released and the request marked FAILED, best-effort.
  - Two truly concurrent first-time submissions for the same
    request_id: usage_repository.reserve()'s advisory-lock + UNIQUE
    (request_id) semantics correctly serialize them so only one gets a
    fresh RESERVED reservation, but the loser previously surfaced that
    as a bare AnalysisInProgressError instead of resolving to the same
    logical analysis, even though the winner is durably creating that
    exact row microseconds away. submit() now catches that specific
    case and polls get_request_by_request_id() (real, DB-uniqueness-
    backed state -- no process-local lock of any kind) for a bounded
    window before giving up and re-raising.
"""
import asyncio
import base64
import binascii
import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import asyncpg

from app.db import analysis_repository
from app.domain.entitlement import AnalysisInProgressError, UsagePolicyService
from app.observability import events as observability_events
from app.queue.base import JobQueue
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore

logger = logging.getLogger(__name__)

ANALYSIS_JOB_TYPE = "analysis"

# Bounded wait for a concurrent winner's create_request()+upload+enqueue
# sequence to become visible to a losing caller of the exact same
# request_id (see module docstring's third bullet). This is real
# Postgres round-trip time, not a fabricated SLA -- generous for even a
# slow local dev DB while still being a bounded, honest "give up and
# tell the truth" rather than a silent hang.
CONCURRENT_REPLAY_POLL_INTERVAL_SECONDS = 0.05
CONCURRENT_REPLAY_POLL_TIMEOUT_SECONDS = 5.0


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
        cell_id: Optional[str] = None,
    ) -> SubmissionResult:
        if not self._async_image_storage_enabled:
            observability_events.analysis_submission(
                request_id=request_id, user_id=str(user_id), outcome="STORAGE_DISABLED",
            )
            raise AsyncImageStorageDisabledError()

        from app.db.consent_repository import REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION, has_valid_consent

        if not await has_valid_consent(self._pool, user_id, REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION):
            observability_events.analysis_submission(
                request_id=request_id, user_id=str(user_id), outcome="CONSENT_REQUIRED",
            )
            raise ConsentRequiredError(REQUIRED_POLICY_VERSION)

        # Idempotent submit (Phase 23): a retried POST with a
        # request_id that already has a durable analysis_requests row
        # returns that row's current state unchanged -- no second
        # reservation, image upload, or job.
        existing = await analysis_repository.get_request_by_request_id(self._pool, user_id, request_id)
        if existing is not None:
            observability_events.analysis_submission(
                request_id=request_id, user_id=str(user_id), outcome="REPLAY",
            )
            return SubmissionResult(analysis_request_id=existing["id"], status=existing["status"], replay=True)

        # QuotaExceededError / AnalysisAlreadyCompletedError propagate
        # uncaught -- same contract as perform_analysis's synchronous
        # path, mapped to HTTP by the v2 route. AnalysisInProgressError
        # is handled specially: it means a truly concurrent submission
        # of this exact request_id is (or just was) creating the
        # durable row this call would otherwise return -- see
        # _await_concurrent_replay().
        try:
            reservation = await self._usage_policy_service.reserve_analysis(user_id, request_id)
        except AnalysisInProgressError as e:
            return await self._await_concurrent_replay(user_id, request_id, e)

        # Strict base64 (`validate=True`): rejects any non-alphabet
        # character outright rather than silently discarding it (the
        # default's permissive behavior), so a corrupted/truncated/
        # wrong-encoding payload fails closed with an honest domain
        # error instead of decoding to unrelated garbage bytes. The
        # original decoder exception is never exposed to the client --
        # only this module's own InvalidImageError message is.
        try:
            image_bytes = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError) as e:
            await self._usage_policy_service.release_reservation(user_id, reservation.id)
            observability_events.analysis_submission(
                request_id=request_id, user_id=str(user_id), outcome="INVALID_IMAGE",
            )
            raise InvalidImageError("image_base64 is not valid base64") from e

        try:
            req = await analysis_repository.create_request(
                self._pool, user_id, request_id,
                usage_reservation_id=reservation.id, home_region=region, cell_id=cell_id,
            )
        except Exception:
            # Failure window A: create_request() never returned a row,
            # so there is no analysis_request to key a later release
            # off of -- release now, or this reservation is stranded
            # RESERVED forever.
            await self._usage_policy_service.release_reservation(user_id, reservation.id)
            observability_events.analysis_submission(request_id=request_id, user_id=str(user_id), outcome="ERROR")
            raise

        try:
            stored = await self._image_store.store(image_bytes, region=region)
        except Exception:
            # Storage is down -- nothing durable to retry from yet
            # (Phase 23's own submission attempt failed outright), so
            # release the slot and mark the request FAILED rather than
            # leaving it stuck in RECEIVED forever.
            await self._usage_policy_service.release_reservation(user_id, reservation.id)
            await analysis_repository.mark_failed(self._pool, user_id, req["id"], "IMAGE_STORAGE_UNAVAILABLE")
            observability_events.analysis_submission(request_id=request_id, user_id=str(user_id), outcome="ERROR")
            raise

        try:
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
        except Exception:
            # Failure window B: the upload succeeded but the atomic
            # mark_queued()+enqueue() transaction did not commit -- see
            # module docstring's second bullet.
            await self._compensate_orphaned_upload(user_id, req["id"], reservation.id, stored)
            observability_events.analysis_submission(request_id=request_id, user_id=str(user_id), outcome="ERROR")
            raise

        observability_events.analysis_submission(request_id=request_id, user_id=str(user_id), outcome="QUEUED")
        return SubmissionResult(analysis_request_id=req["id"], status="QUEUED", replay=False)

    async def _compensate_orphaned_upload(
        self, user_id: UUID, analysis_request_id: UUID, reservation_id: UUID, stored,
    ) -> None:
        """Compensation for failure window B (see module docstring).
        Runs each of its three steps independently -- a failure in one
        (e.g. the DB being the very thing that's down) must not skip
        the others, and every one of them is itself best-effort against
        a system that just failed once already."""
        confirmed_deleted = False
        try:
            confirmed_deleted = await self._image_store.delete_if_confirmed(stored.object_key)
        except Exception:
            logger.warning(
                "analysis_submission: orphan-cleanup delete raised for object_key=%s (analysis_request_id=%s)",
                stored.object_key, analysis_request_id, exc_info=True,
            )

        if not confirmed_deleted:
            # Preserve the reference *outside* the transaction that
            # just failed, so the cleanup sweeper (driven purely by
            # image_expires_at, independent of this request's status)
            # can recover it later. A safe, honest "cleanup pending" --
            # never silently forgotten.
            try:
                await analysis_repository.record_ephemeral_image_reference(
                    self._pool, user_id, analysis_request_id,
                    image_object_key=stored.object_key, image_expires_at=stored.expires_at,
                )
                logger.warning(
                    "analysis_submission: enqueue failed after upload, object_key=%s left for cleanup sweeper "
                    "(analysis_request_id=%s)",
                    stored.object_key, analysis_request_id,
                )
            except Exception:
                logger.error(
                    "analysis_submission: could not even record the orphaned image reference for object_key=%s "
                    "(analysis_request_id=%s) -- object may be unrecoverable by the sweeper",
                    stored.object_key, analysis_request_id, exc_info=True,
                )

        try:
            await self._usage_policy_service.release_reservation(user_id, reservation_id)
        except Exception:
            logger.error(
                "analysis_submission: could not release usage reservation for analysis_request_id=%s",
                analysis_request_id, exc_info=True,
            )

        try:
            await analysis_repository.mark_failed(self._pool, user_id, analysis_request_id, "ENQUEUE_FAILED")
        except Exception:
            logger.error(
                "analysis_submission: could not mark analysis_request_id=%s FAILED after enqueue failure",
                analysis_request_id, exc_info=True,
            )

    async def _await_concurrent_replay(
        self, user_id: UUID, request_id: str, original_error: AnalysisInProgressError,
    ) -> "SubmissionResult":
        """A concurrent winner is (or was, microseconds ago) creating
        the durable analysis_requests row for this exact request_id --
        poll for it rather than surfacing a bare in-progress error to
        every losing caller. Backed entirely by real DB state
        (analysis_requests.request_id is UNIQUE) -- no process-local
        lock, no in-memory coordination; this works correctly across
        multiple worker/API processes for exactly that reason."""
        deadline = asyncio.get_event_loop().time() + CONCURRENT_REPLAY_POLL_TIMEOUT_SECONDS
        while True:
            existing = await analysis_repository.get_request_by_request_id(self._pool, user_id, request_id)
            if existing is not None:
                return SubmissionResult(analysis_request_id=existing["id"], status=existing["status"], replay=True)
            if asyncio.get_event_loop().time() >= deadline:
                # The winner's reservation exists but its
                # analysis_requests row still doesn't, after a generous
                # bounded wait -- something is stuck (not merely slow).
                # Honest failure, not an infinite wait: re-raise the
                # original, real error rather than fabricating a new one.
                raise original_error
            await asyncio.sleep(CONCURRENT_REPLAY_POLL_INTERVAL_SECONDS)
