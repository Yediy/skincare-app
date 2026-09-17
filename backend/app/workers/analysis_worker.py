"""Part VI's real queue-consuming CV worker. Entry point:

    python -m app.workers.analysis_worker

Loop, per job: claim() -> a background heartbeat task keeps extending
visibility while AnalysisExecutionService.execute() runs the CV
pipeline -> acknowledge() on success, or fail() with a classified
retryable/terminal decision on failure -> on a terminal outcome
(non-retryable, or retries exhausted -- dead letter), release quota
and mark the durable request FAILED via
AnalysisExecutionService.mark_terminal_failure().

No permanent raw image storage anywhere in this module: the object is
only ever held as in-memory bytes for the duration of one execute()
call; deletion (success or terminal failure) happens inside
AnalysisExecutionService itself, with the cleanup sweeper
(app/workers/image_cleanup.py) as the independent safety net either
way.

Does not hold a Postgres transaction open across the CV pipeline call
-- claim()/acknowledge()/fail()/extend_visibility() are each their own
short-lived transaction (PostgresJobQueue), and AnalysisExecutionService
opens its own transactions only around discrete DB operations, never
around compute.
"""
import asyncio
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Optional, Tuple
from uuid import UUID

from app.domain.analysis_execution_service import (
    AnalysisExecutionLeaseLostError,
    AnalysisExecutionService,
    AnalysisRequestInvalidStateError,
    AnalysisRequestNotFoundError,
    ImageRetrievalError,
)
from app.cv.pipeline import CaptureQualityFailedError, NoFaceDetectedError
from app.observability import events as observability_events
from app.queue.base import Job, JobLeaseLostError, JobNotFoundError, JobQueue
from app.storage.base import ObjectNotFoundError

logger = logging.getLogger(__name__)

ANALYSIS_JOB_TYPE = "analysis"

# How long a claim is valid before another worker may reclaim it if
# this one never heartbeats again (crash, kill -9). Generous for a CV
# pipeline run (seconds, in practice -- see PostgresJobQueue's own
# docstring), while still being bounded, not "forever".
CLAIM_VISIBILITY_TIMEOUT_SECONDS = 300
# Heartbeat cadence and the extension it applies each beat -- beats
# well inside the claim timeout above so a single missed tick (a GC
# pause, a slow DB round trip) doesn't cost the claim.
HEARTBEAT_INTERVAL_SECONDS = 60
HEARTBEAT_EXTENSION_SECONDS = 300
# How long to sleep between claim attempts when the queue is empty --
# a plain poll loop, not a fabricated pub/sub mechanism; this is the
# launch implementation app/queue/base.py's own docstring describes
# (a Postgres-table-backed queue, not Kafka/SQS).
EMPTY_QUEUE_POLL_INTERVAL_SECONDS = 2.0

# Closed set of exception types this worker treats as TERMINAL --
# retrying will not change the outcome. Everything else defaults to
# RETRYABLE (bounded by the job's own max_attempts, so an
# unanticipated failure mode still dead-letters eventually rather than
# retrying forever) -- see RETRYABLE examples in the module docstring:
# temporary Postgres/R2/infrastructure failures are exactly the
# "everything else" this default is for.
_TERMINAL_ERROR_CODES = {
    NoFaceDetectedError: "NO_FACE_DETECTED",
    CaptureQualityFailedError: "CAPTURE_QUALITY_FAILED",
    ValueError: "INVALID_IMAGE",
    AnalysisRequestNotFoundError: "ANALYSIS_REQUEST_NOT_FOUND",
    AnalysisRequestInvalidStateError: "INVALID_REQUEST_STATE",
}


def classify_failure(exc: Exception) -> Tuple[bool, str]:
    """Returns (retryable, error_code). error_code is always one of
    the safe, client-facing classifications app/api/v2/analyses.py's
    GET route already recognizes -- never an exception message, never
    a stack trace."""
    if isinstance(exc, ImageRetrievalError):
        if isinstance(exc.__cause__, ObjectNotFoundError):
            # The object is genuinely gone (most likely its retention
            # window already expired) -- retrying will see the exact
            # same absence.
            return False, "IMAGE_NOT_FOUND"
        return True, "IMAGE_STORAGE_UNAVAILABLE"

    for exc_type, error_code in _TERMINAL_ERROR_CODES.items():
        if isinstance(exc, exc_type):
            return False, error_code

    return True, "PROCESSING_FAILED"


async def _heartbeat_loop(job_queue: JobQueue, job_id: UUID, claim_token: UUID) -> None:
    """Runs alongside the CV pipeline call, periodically extending the
    claim's visibility so another worker doesn't reclaim a job that is
    simply taking longer than CLAIM_VISIBILITY_TIMEOUT_SECONDS to
    process -- Part VI, Phase 28's heartbeat requirement. Cancelled
    (not awaited to completion) by the caller once the real work
    finishes; CancelledError is the expected, clean shutdown path
    here, not an error."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            await job_queue.extend_visibility(job_id, claim_token, HEARTBEAT_EXTENSION_SECONDS)
        except JobNotFoundError:
            # No longer claimed by anyone -- already acknowledged/
            # failed by the main task, or (a real, if unlikely, race)
            # already reclaimed after this loop fell behind. Either
            # way, nothing more for the heartbeat to do.
            return
        except JobLeaseLostError:
            # System Integrity Gate V1: still claimed, but by a newer
            # token -- this worker's lease is already gone. Stop
            # heartbeating a job that isn't this worker's to hold
            # visible anymore; _process_job's own fenced acknowledge/
            # fail calls will discover the same thing independently.
            return


async def _process_job(job: Job, job_queue: JobQueue, execution_service: AnalysisExecutionService) -> None:
    heartbeat_task = asyncio.create_task(_heartbeat_loop(job_queue, job.id, job.claim_token))
    started_at = time.monotonic()
    try:
        analysis_request_id = UUID(job.payload["analysis_request_id"])
        user_id = UUID(job.payload["user_id"])

        try:
            outcome = await execution_service.execute(user_id, analysis_request_id, job.claim_token, job.id)
            await job_queue.acknowledge(job.id, job.claim_token)
            logger.info(
                "analysis_worker: job=%s analysis_request_id=%s completed already_completed=%s",
                job.id, analysis_request_id, outcome.already_completed,
            )
            observability_events.processing_result(
                job_id=str(job.id), analysis_id=str(analysis_request_id),
                outcome="SUCCESS", duration_seconds=time.monotonic() - started_at,
            )
        except (AnalysisExecutionLeaseLostError, JobLeaseLostError) as e:
            # System Integrity Gate V1: this worker's lease -- queue-
            # level, execution-level, or both -- was already lost to a
            # newer worker by the time it got here. STALE ATTEMPT /
            # ABANDON: never a retryable processing failure (it would
            # requeue a job someone else already legitimately owns) and
            # never a terminal failure (it would mark FAILED, release
            # quota, or delete an image out from under whoever now
            # legitimately owns this request). No compensation of any
            # kind -- the newer worker owns this outcome.
            logger.warning(
                "analysis_worker: job=%s analysis_request_id=%s abandoned -- %s",
                job.id, analysis_request_id, e.__class__.__name__,
            )
            observability_events.processing_result(
                job_id=str(job.id), analysis_id=str(analysis_request_id),
                outcome="ABANDONED", duration_seconds=time.monotonic() - started_at,
            )
        except Exception as e:
            retryable, error_code = classify_failure(e)
            try:
                is_terminal = await job_queue.fail(
                    job.id, job.claim_token, f"{e.__class__.__name__}: {error_code}", retryable=retryable,
                )
            except JobLeaseLostError:
                logger.warning(
                    "analysis_worker: job=%s analysis_request_id=%s abandoned -- lease lost while reporting failure",
                    job.id, analysis_request_id,
                )
                observability_events.processing_result(
                    job_id=str(job.id), analysis_id=str(analysis_request_id),
                    outcome="ABANDONED", duration_seconds=time.monotonic() - started_at,
                )
                return
            logger.warning(
                "analysis_worker: job=%s analysis_request_id=%s failed error_code=%s retryable=%s terminal=%s",
                job.id, analysis_request_id, error_code, retryable, is_terminal,
            )
            observability_events.processing_result(
                job_id=str(job.id), analysis_id=str(analysis_request_id),
                outcome="DEAD_LETTER" if is_terminal else "RETRY",
                duration_seconds=time.monotonic() - started_at, error_code=error_code,
            )
            if is_terminal:
                # Dead letter (Part VI, Phase 29): either this
                # exception was never retryable, or retries are now
                # exhausted. Release quota + mark the durable request
                # FAILED exactly once, here -- unless this worker's
                # execution-level lease was itself already lost (a
                # newer worker installed its own processing_claim_token
                # in the meantime), in which case abandon instead of
                # compensating against a request that isn't this
                # worker's anymore.
                try:
                    await execution_service.mark_terminal_failure(
                        user_id, analysis_request_id, error_code, job.claim_token,
                    )
                except AnalysisExecutionLeaseLostError:
                    logger.warning(
                        "analysis_worker: job=%s analysis_request_id=%s terminal-failure compensation abandoned -- lease lost",
                        job.id, analysis_request_id,
                    )
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


async def run_one_cycle(job_queue: JobQueue, execution_service: AnalysisExecutionService) -> bool:
    """Claims and processes at most one job. Returns True if a job was
    claimed (regardless of whether it ultimately succeeded or failed),
    False if the queue had nothing pending-and-ready."""
    job = await job_queue.claim(ANALYSIS_JOB_TYPE, visibility_timeout_seconds=CLAIM_VISIBILITY_TIMEOUT_SECONDS)
    observability_events.queue_claim(
        job_type=ANALYSIS_JOB_TYPE, job_id=str(job.id) if job else None, claimed=job is not None,
    )
    if job is None:
        return False
    observability_events.queue_wait_seconds(
        job_id=str(job.id),
        wait_seconds=(datetime.now(timezone.utc) - job.created_at).total_seconds(),
    )
    await _process_job(job, job_queue, execution_service)
    return True


async def run_forever(
    job_queue: JobQueue,
    execution_service: AnalysisExecutionService,
    *,
    poll_interval_seconds: float = EMPTY_QUEUE_POLL_INTERVAL_SECONDS,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """`stop_event`, when given, is checked between cycles -- set it
    (e.g. from a SIGTERM/SIGINT handler, see _main's
    _install_graceful_shutdown) to let the current job finish and then
    return, instead of claiming another one. None (the default)
    preserves the original unconditional-loop behavior for any
    existing caller (e.g. tests) that never passes one."""
    while stop_event is None or not stop_event.is_set():
        claimed = await run_one_cycle(job_queue, execution_service)
        if not claimed:
            await asyncio.sleep(poll_interval_seconds)


def _install_graceful_shutdown(stop_event: asyncio.Event) -> None:
    """SIGTERM (the signal a container/process manager sends for an
    ordinary graceful stop) and SIGINT (Ctrl-C during local dev) both
    just set stop_event -- run_forever finishes whatever job it's
    already processing, then returns, so _main's own `finally` gets a
    chance to shut down the execution service's owned CV executor and
    the database pool cleanly instead of having them vanish out from
    under an in-flight request via a hard process kill."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # add_signal_handler is POSIX-only (no-op on e.g. Windows);
            # graceful shutdown becomes best-effort there rather than a
            # hard requirement for a platform this worker doesn't ship
            # on.
            pass


async def _main() -> None:
    from app.config import settings
    from app.db.connection import init_db_pool, get_db_pool, close_db_pool
    from app.domain.entitlement import UsagePolicyService, build_entitlement_service
    from app.domain.product_matching_service import ProductMatchingService
    from app.domain.safety_engine import SafetyEngine
    from app.cv.pipeline import FacialAnalysisPipeline
    from app.ml.scorer import FacialScorer
    from app.queue.postgres_queue import PostgresJobQueue
    from app.services.plan_service import PlanService
    from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore
    from app.storage.r2 import CloudflareR2ObjectStorage

    await init_db_pool()
    execution_service: Optional[AnalysisExecutionService] = None
    try:
        pool = get_db_pool()
        safety_engine = SafetyEngine()
        object_storage = CloudflareR2ObjectStorage(
            account_id=settings.r2_account_id or "",
            access_key_id=settings.r2_access_key_id or "",
            secret_access_key=settings.r2_secret_access_key or "",
            bucket=settings.r2_bucket or "",
            endpoint_url=settings.r2_endpoint,
        )
        execution_service = AnalysisExecutionService(
            pool,
            pipeline=FacialAnalysisPipeline(),
            scorer=FacialScorer(),
            plan_service=PlanService(),
            usage_policy_service=UsagePolicyService(pool, build_entitlement_service(pool)),
            product_matching_service=ProductMatchingService(pool, safety_engine),
            safety_engine=safety_engine,
            image_store=EphemeralAnalysisImageStore(object_storage),
        )
        job_queue = PostgresJobQueue(pool)
        stop_event = asyncio.Event()
        _install_graceful_shutdown(stop_event)
        logger.info("analysis_worker: starting main loop")
        await run_forever(job_queue, execution_service, stop_event=stop_event)
        logger.info("analysis_worker: graceful shutdown signal received, main loop exited")
    finally:
        # Executor lifecycle (System Integrity Gate V1, section 5):
        # this service owns the CV ThreadPoolExecutor it constructed
        # for itself above (no cv_executor= was injected), so this
        # process -- not the service's own __del__, and not implicit
        # process teardown -- is what must shut it down, and it must
        # happen before the database pool underneath it disappears.
        if execution_service is not None:
            execution_service.close()
        await close_db_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
