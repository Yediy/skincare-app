"""AnalysisExecutionService (Part VI): executes one already-submitted,
durable analysis request. The async worker's counterpart to
app.domain.analysis_service.perform_analysis -- both wrap the same
quota-independent compute_analysis(), but perform_analysis owns a
*fresh* reservation lifecycle (reserve at the start, consume/release
at the end) while this service reuses the reservation
AnalysisSubmissionService already created at submission time.

Quota ownership, explicit rather than implicit (this pass's own
required distinction):
    SUBMISSION (AnalysisSubmissionService) reserves.
    EXECUTION (this module) consumes (via commit_analysis_result's own
    atomic UPDATE, on success) or releases (via mark_terminal_failure,
    on a terminal failure only -- never on a retryable one, which
    keeps its slot RESERVED for the next attempt).
This module never calls UsagePolicyService.reserve_analysis() -- if it
ever needs to, that is a sign something has gone wrong upstream, not a
gap to quietly paper over here.

Called only by app/workers/analysis_worker.py, never directly by an
HTTP route -- the worker owns retry classification/dead-lettering
(which of this module's exceptions are retryable is that module's
concern, not this one's).

Heartbeat-starvation fix: the worker's visibility heartbeat
(app/workers/analysis_worker.py's _heartbeat_loop) is a plain asyncio
task on the same event loop this service's execute() runs on. A
synchronous, multi-second MediaPipe/OpenCV call sitting directly on
that loop would starve the heartbeat for its whole duration -- long
enough, under a short enough visibility timeout, for a second worker
to legitimately reclaim a job the first is still actively processing.
execute() passes its own dedicated single-thread executor (see
__init__ below) into compute_analysis() so the CV call runs off-loop;
scoring/planning/DB work after it stays on the loop unchanged.
"""
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import asyncpg

from app.db import analysis_repository
from app.domain.analysis_service import compute_analysis
from app.domain.entitlement import UsagePolicyService
from app.domain.product_matching_service import ProductMatchingService
from app.domain.safety_engine import SafetyEngine
from app.cv.pipeline import FacialAnalysisPipeline
from app.ml.scorer import FacialScorer
from app.services.plan_service import PlanService
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore

EXECUTABLE_STATUSES = ("QUEUED", "PROCESSING")


class AnalysisRequestNotFoundError(Exception):
    """The durable analysis_requests row this job pointed at doesn't
    exist. Terminal, not retryable -- there is no plausible future
    state in which retrying makes this row appear; the job payload
    itself is wrong."""


class AnalysisRequestInvalidStateError(Exception):
    """The request exists but isn't in a state this service can
    execute from: already FAILED/CANCELLED, or has no image reference
    at all. Terminal, not retryable -- this is a data/state
    inconsistency, not a transient condition."""


class ImageRetrievalError(Exception):
    """Raised when the ephemeral image object could not be retrieved.
    The original storage exception is chained (`__cause__`) so the
    worker's retry classification can distinguish ObjectNotFoundError
    (terminal -- the object is genuinely gone, most likely its
    retention window already expired) from
    ObjectStorageUnavailableError (retryable -- the backend was simply
    unreachable this attempt)."""


@dataclass(frozen=True)
class ExecutionOutcome:
    analysis_request_id: UUID
    # True if this call found the request already COMPLETED and did
    # nothing further -- the required "retry after durable completion
    # does not recompute" guarantee. A worker that re-claims a job
    # whose result transaction already committed (e.g. it crashed
    # between commit_analysis_result() and acknowledge()) hits this
    # path, not a second compute/consumption.
    already_completed: bool


class AnalysisExecutionService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        pipeline: FacialAnalysisPipeline,
        scorer: FacialScorer,
        plan_service: PlanService,
        usage_policy_service: UsagePolicyService,
        product_matching_service: ProductMatchingService,
        safety_engine: SafetyEngine,
        image_store: EphemeralAnalysisImageStore,
        cv_executor: Optional[Executor] = None,
    ):
        self._pool = pool
        self._pipeline = pipeline
        self._scorer = scorer
        self._plan_service = plan_service
        self._usage_policy_service = usage_policy_service
        self._product_matching_service = product_matching_service
        self._safety_engine = safety_engine
        self._image_store = image_store
        # Dedicated, single-thread, owned by this one worker process's
        # AnalysisExecutionService instance for its entire lifetime --
        # deliberately not the default asyncio.to_thread pool (which
        # draws from several threads with no guarantee a given call
        # lands on the same one as the last). `self._pipeline`'s
        # underlying MediaPipe FaceMesh/tflite objects are not
        # documented as safe to invoke from arbitrary/varying threads;
        # a single fixed thread sidesteps that question entirely
        # rather than assuming the answer is "yes". Still only ever one
        # call in flight at a time either way -- this worker already
        # processes one job at a time (see app/workers/analysis_worker.
        # py's run_forever) -- so max_workers=1 costs nothing and
        # removes a class of risk for free. See compute_analysis()'s
        # cv_executor docstring for the other half of this fix.
        self._cv_executor = cv_executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="analysis-cv"
        )

    async def execute(self, user_id: UUID, analysis_request_id: UUID) -> ExecutionOutcome:
        req = await analysis_repository.get_request_by_id(self._pool, user_id, analysis_request_id)
        if req is None:
            raise AnalysisRequestNotFoundError(str(analysis_request_id))

        if req["status"] == "COMPLETED":
            return ExecutionOutcome(analysis_request_id=analysis_request_id, already_completed=True)

        if req["status"] not in EXECUTABLE_STATUSES:
            raise AnalysisRequestInvalidStateError(
                f"analysis_request {analysis_request_id} is {req['status']}, not executable"
            )
        if not req["image_object_key"]:
            raise AnalysisRequestInvalidStateError(
                f"analysis_request {analysis_request_id} has no image reference"
            )

        # Idempotent to call again on a retry (just re-sets status/
        # started_at) -- a job that failed retryably after this point
        # last attempt re-enters here exactly the same way.
        await analysis_repository.mark_processing(self._pool, user_id, analysis_request_id)

        try:
            image_bytes = await self._image_store.retrieve(req["image_object_key"])
        except Exception as e:
            raise ImageRetrievalError(str(analysis_request_id)) from e

        # NoFaceDetectedError / CaptureQualityFailedError / ValueError
        # propagate unchanged from here -- the worker's retry
        # classification treats all three as terminal (a bad capture
        # will not improve on retry).
        result = await compute_analysis(
            self._pool, user_id, image_bytes,
            pipeline=self._pipeline, scorer=self._scorer, plan_service=self._plan_service,
            product_matching_service=self._product_matching_service, safety_engine=self._safety_engine,
            cv_executor=self._cv_executor,
        )

        # Part VI, Phase 30's atomic commit: result + measurements +
        # product recommendations + request COMPLETED + quota CONSUMED,
        # all in one transaction, idempotent via ON CONFLICT DO NOTHING
        # -- a retry after this call already committed re-runs
        # everything above (wasted compute, never wasted correctness)
        # but writes nothing twice.
        await analysis_repository.commit_analysis_result(
            self._pool, user_id, analysis_request_id,
            capture_assessment=result.capture_assessment,
            scores=result.scores,
            plan=result.plan,
            eligible_for_longitudinal_comparison=result.eligible_for_longitudinal_comparison,
            pipeline_version=result.capture_assessment.get("capture_pipeline_version") or "unknown",
            metric_results=result.metric_results,
            product_recommendations=result.product_recommendations,
            usage_reservation_id=req["usage_reservation_id"],
        )

        # Primary raw-image deletion path (Part IV, Phase 20) -- best
        # effort. If this fails, the result is already durably
        # COMPLETED and stays that way; the DB-side reference is left
        # in place for the cleanup sweeper to recover later. Never
        # roll back a valid, committed analysis result merely because
        # object-storage deletion happened to fail right now.
        confirmed = await self._image_store.delete_if_confirmed(req["image_object_key"])
        if confirmed:
            await analysis_repository.clear_image_reference(self._pool, analysis_request_id)

        return ExecutionOutcome(analysis_request_id=analysis_request_id, already_completed=False)

    async def mark_terminal_failure(self, user_id: UUID, analysis_request_id: UUID, error_code: str) -> None:
        """Called by the worker once it has decided a failure is
        terminal -- either genuinely non-retryable, or a retryable
        one whose attempts are now exhausted (dead letter, Part VI
        Phase 29). Releases the reservation (a no-op if it's already
        CONSUMED/RELEASED -- see usage_repository.release()), marks
        the durable request FAILED, and best-effort deletes the raw
        image. Every step is independently safe to call more than
        once, so a worker that dies partway through this and picks the
        dead-lettered job's cleanup back up (or the cleanup sweeper,
        for the image specifically) never double-charges or corrupts
        state."""
        req = await analysis_repository.get_request_by_id(self._pool, user_id, analysis_request_id)
        if req is not None and req.get("usage_reservation_id") is not None:
            await self._usage_policy_service.release_reservation(user_id, req["usage_reservation_id"])

        await analysis_repository.mark_failed(self._pool, user_id, analysis_request_id, error_code)

        if req is not None and req.get("image_object_key"):
            confirmed = await self._image_store.delete_if_confirmed(req["image_object_key"])
            if confirmed:
                await analysis_repository.clear_image_reference(self._pool, analysis_request_id)
