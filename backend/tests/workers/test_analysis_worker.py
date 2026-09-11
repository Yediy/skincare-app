"""app.workers.analysis_worker -- Part VI's real queue-consuming CV
worker: claim/heartbeat/execute/ack-or-classify-and-fail, against a
real PostgresJobQueue and (for the full-pipeline tests) the real CV
singletons. Only object storage is faked, same pattern as
tests/domain/test_analysis_execution_service.py.
"""
import asyncio
import base64
import io
import time
import uuid
from pathlib import Path

import pytest
from PIL import Image

from app.db import analysis_repository
from app.domain.analysis_execution_service import AnalysisExecutionService, ExecutionOutcome
from app.domain.analysis_submission_service import AnalysisSubmissionService
from app.domain.entitlement import FreeTierEntitlementService, UsagePolicyService
from app.domain.product_matching_service import ProductMatchingService
from app.queue.postgres_queue import PostgresJobQueue
from app.storage.base import ObjectNotFoundError, ObjectStorage, ObjectStorageUnavailableError
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore
from app.workers import analysis_worker

GRACE_HOPPER_JPG = Path(__file__).resolve().parent.parent / "fixtures" / "grace_hopper.jpg"


class FakeObjectStorage(ObjectStorage):
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.unavailable = False

    async def put(self, key, data, *, content_type=None):
        if self.unavailable:
            raise ObjectStorageUnavailableError("fake unavailable")
        self.objects[key] = data

    async def get(self, key):
        if self.unavailable:
            raise ObjectStorageUnavailableError("fake unavailable")
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]

    async def delete(self, key):
        if self.unavailable:
            raise ObjectStorageUnavailableError("fake unavailable")
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        del self.objects[key]

    async def exists(self, key):
        return key in self.objects

    async def create_upload_authorization(self, key, *, content_type=None, expires_in_seconds=900):
        raise NotImplementedError

    async def create_download_authorization(self, key, *, expires_in_seconds=900):
        raise NotImplementedError


class _StubExecutionService:
    """Duck-typed AnalysisExecutionService stand-in -- for the
    heartbeat and dead-letter tests, which need deterministic timing/
    failure behavior, not a real CV run."""

    def __init__(self, *, delay_seconds: float = 0.0, raise_exc: Exception | None = None):
        self.delay_seconds = delay_seconds
        self.raise_exc = raise_exc
        self.execute_calls = 0
        self.terminal_failure_calls = []

    async def execute(self, user_id, analysis_request_id):
        self.execute_calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.raise_exc is not None:
            raise self.raise_exc
        return ExecutionOutcome(analysis_request_id=analysis_request_id, already_completed=False)

    async def mark_terminal_failure(self, user_id, analysis_request_id, error_code):
        self.terminal_failure_calls.append((user_id, analysis_request_id, error_code))


async def _create_user_with_consent(db_pool, email: str):
    from app.db.consent_repository import REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION, record_consent

    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    user_id = row["id"]
    await record_consent(db_pool, user_id, REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION, "testing")
    return user_id


def _blank_image_b64() -> str:
    blank = Image.new("RGB", (200, 200), color=(128, 128, 128))
    buf = io.BytesIO()
    blank.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


async def _submit(app_db_pool, user_id, image_b64, fake_storage, allowance=5):
    usage_policy_service = UsagePolicyService(app_db_pool, FreeTierEntitlementService(allowance))
    submission_service = AnalysisSubmissionService(
        app_db_pool,
        usage_policy_service=usage_policy_service,
        image_store=EphemeralAnalysisImageStore(fake_storage),
        job_queue=PostgresJobQueue(app_db_pool),
        async_image_storage_enabled=True,
    )
    request_id = str(uuid.uuid4())
    result = await submission_service.submit(user_id, request_id, image_b64)
    return result, request_id


def _real_execution_service(app_db_pool, fake_storage):
    from app.main import pipeline, plan_service, safety_engine, scorer

    usage_policy_service = UsagePolicyService(app_db_pool, FreeTierEntitlementService())
    return AnalysisExecutionService(
        app_db_pool,
        pipeline=pipeline, scorer=scorer, plan_service=plan_service,
        usage_policy_service=usage_policy_service,
        product_matching_service=ProductMatchingService(app_db_pool, safety_engine),
        safety_engine=safety_engine,
        image_store=EphemeralAnalysisImageStore(fake_storage),
    )


class _BlockingWrapperPipeline:
    """Wraps the real FacialAnalysisPipeline singleton, inserting a
    genuinely blocking time.sleep() before delegating to it.

    This is deliberately NOT an asyncio.sleep()-based stub (see
    _StubExecutionService above): asyncio.sleep() yields control back
    to the event loop, so a test built on it can never actually
    reproduce the real failure mode this pass fixes -- a real
    MediaPipe/OpenCV call is synchronous and does not yield at all.
    time.sleep() here genuinely blocks whichever thread calls
    analyze(), exactly like real CV work would. Delegating to the real
    pipeline afterward (rather than returning a fabricated result)
    means everything downstream of extraction -- scoring, planning,
    product matching, the atomic commit -- runs for real too, so this
    test proves the whole path, not just the timing."""

    def __init__(self, real_pipeline, delay_seconds: float):
        self._real_pipeline = real_pipeline
        self._delay_seconds = delay_seconds
        self.analyze_calls = 0

    def analyze(self, image_bytes: bytes):
        self.analyze_calls += 1
        time.sleep(self._delay_seconds)
        return self._real_pipeline.analyze(image_bytes)


def _real_execution_service_with_blocking_cv(app_db_pool, fake_storage, *, delay_seconds: float):
    from app.main import pipeline, plan_service, safety_engine, scorer

    usage_policy_service = UsagePolicyService(app_db_pool, FreeTierEntitlementService())
    blocking_pipeline = _BlockingWrapperPipeline(pipeline, delay_seconds)
    execution_service = AnalysisExecutionService(
        app_db_pool,
        pipeline=blocking_pipeline, scorer=scorer, plan_service=plan_service,
        usage_policy_service=usage_policy_service,
        product_matching_service=ProductMatchingService(app_db_pool, safety_engine),
        safety_engine=safety_engine,
        image_store=EphemeralAnalysisImageStore(fake_storage),
    )
    return execution_service, blocking_pipeline


async def test_worker_claims_and_completes_a_real_analysis_job(db_pool, app_db_pool):
    user_id = await _create_user_with_consent(db_pool, "worker-happy@test.com")
    fake_storage = FakeObjectStorage()
    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
    submitted, request_id = await _submit(app_db_pool, user_id, image_b64, fake_storage)

    job_queue = PostgresJobQueue(app_db_pool)
    execution_service = _real_execution_service(app_db_pool, fake_storage)

    claimed = await analysis_worker.run_one_cycle(job_queue, execution_service)
    assert claimed is True

    job = await db_pool.fetchrow("SELECT status FROM jobs WHERE request_id = $1", request_id)
    assert job["status"] == "completed"
    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["status"] == "COMPLETED"
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "CONSUMED"

    # Nothing left to claim now.
    assert await analysis_worker.run_one_cycle(job_queue, execution_service) is False


async def test_bad_image_terminal_failure_does_not_retry(db_pool, app_db_pool):
    user_id = await _create_user_with_consent(db_pool, "worker-badimage@test.com")
    fake_storage = FakeObjectStorage()
    submitted, request_id = await _submit(app_db_pool, user_id, _blank_image_b64(), fake_storage)

    job_queue = PostgresJobQueue(app_db_pool)
    execution_service = _real_execution_service(app_db_pool, fake_storage)

    await analysis_worker.run_one_cycle(job_queue, execution_service)

    job = await db_pool.fetchrow("SELECT status, attempt_count FROM jobs WHERE request_id = $1", request_id)
    assert job["status"] == "failed"  # terminal on the very first attempt, never requeued
    assert job["attempt_count"] == 1

    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["status"] == "FAILED"
    assert req["error_code"] == "NO_FACE_DETECTED"
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RELEASED"


async def test_temporary_storage_failure_retries_without_releasing_quota(db_pool, app_db_pool):
    user_id = await _create_user_with_consent(db_pool, "worker-storagedown@test.com")
    fake_storage = FakeObjectStorage()
    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
    submitted, request_id = await _submit(app_db_pool, user_id, image_b64, fake_storage)

    # Storage goes down only *after* submission (the image already
    # uploaded fine) -- retrieval at execution time fails transiently.
    fake_storage.unavailable = True

    job_queue = PostgresJobQueue(app_db_pool)
    execution_service = _real_execution_service(app_db_pool, fake_storage)

    await analysis_worker.run_one_cycle(job_queue, execution_service)

    job = await db_pool.fetchrow(
        "SELECT status, attempt_count, next_attempt_at FROM jobs WHERE request_id = $1", request_id
    )
    assert job["status"] == "pending"  # requeued, not dead-lettered
    assert job["attempt_count"] == 1
    assert job["next_attempt_at"] is not None

    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["status"] != "FAILED"  # never marked failed on a retryable outcome
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RESERVED"  # never released on a retryable outcome


async def test_max_attempts_exhausted_dead_letters_and_releases_quota(db_pool, app_db_pool):
    user_id = await _create_user_with_consent(db_pool, "worker-deadletter@test.com")
    fake_storage = FakeObjectStorage()
    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
    submitted, request_id = await _submit(app_db_pool, user_id, image_b64, fake_storage)

    # Force this job's very first failure to already exhaust attempts,
    # without waiting through real exponential backoff.
    await db_pool.execute("UPDATE jobs SET max_attempts = 1 WHERE request_id = $1", request_id)

    job_queue = PostgresJobQueue(app_db_pool)
    stub_execution = _StubExecutionService(raise_exc=RuntimeError("simulated unclassified infra failure"))

    await analysis_worker._process_job(
        await job_queue.claim(analysis_worker.ANALYSIS_JOB_TYPE), job_queue, stub_execution,
    )

    job = await db_pool.fetchrow("SELECT status FROM jobs WHERE request_id = $1", request_id)
    assert job["status"] == "failed"
    assert len(stub_execution.terminal_failure_calls) == 1
    assert stub_execution.terminal_failure_calls[0][2] == "PROCESSING_FAILED"


async def test_heartbeat_prevents_a_concurrent_worker_from_reclaiming(db_pool, app_db_pool, monkeypatch):
    """A job that takes longer to process than its original claim
    visibility window must not be reclaimable by a second worker as
    long as the first is still alive and heartbeating."""
    user_id = await _create_user_with_consent(db_pool, "worker-heartbeat@test.com")
    fake_storage = FakeObjectStorage()
    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
    _, request_id = await _submit(app_db_pool, user_id, image_b64, fake_storage)

    monkeypatch.setattr(analysis_worker, "CLAIM_VISIBILITY_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(analysis_worker, "HEARTBEAT_INTERVAL_SECONDS", 0.3)
    monkeypatch.setattr(analysis_worker, "HEARTBEAT_EXTENSION_SECONDS", 5)

    job_queue_a = PostgresJobQueue(app_db_pool)
    job_queue_b = PostgresJobQueue(app_db_pool)  # simulates a second worker process
    slow_execution = _StubExecutionService(delay_seconds=1.5)

    worker_task = asyncio.create_task(analysis_worker.run_one_cycle(job_queue_a, slow_execution))
    await asyncio.sleep(1.2)  # past the original 1s visibility window, heartbeat should have extended it

    reclaimed = await job_queue_b.claim(analysis_worker.ANALYSIS_JOB_TYPE, visibility_timeout_seconds=1)
    assert reclaimed is None  # heartbeat kept it claimed by worker A

    claimed = await worker_task
    assert claimed is True
    assert slow_execution.execute_calls == 1

    job = await db_pool.fetchrow("SELECT status FROM jobs WHERE request_id = $1", request_id)
    assert job["status"] == "completed"


async def test_heartbeat_survives_a_genuinely_blocking_synchronous_cv_call(db_pool, app_db_pool, monkeypatch):
    """The heartbeat test above uses asyncio.sleep() as its stand-in
    for "slow work" -- which yields control back to the event loop, so
    it cannot actually reproduce the real bug this pass fixes: a real
    MediaPipe/OpenCV call is synchronous and never yields. Before the
    fix, running that call directly on the worker's event-loop thread
    would starve _heartbeat_loop for the call's entire duration, and a
    second worker could legitimately reclaim a job the first was still
    actively processing.

    This test runs the REAL execution/worker path (AnalysisExecutionService
    + run_one_cycle, not the duck-typed _StubExecutionService) with a
    pipeline whose analyze() genuinely blocks its calling thread via
    time.sleep() for longer than the configured claim visibility --
    proving the heartbeat still fires (because the fix moves that
    blocking call onto a dedicated executor thread, off the event
    loop) and a concurrent second worker still cannot reclaim the job.
    """
    user_id = await _create_user_with_consent(db_pool, "worker-blocking-cv@test.com")
    fake_storage = FakeObjectStorage()
    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
    _, request_id = await _submit(app_db_pool, user_id, image_b64, fake_storage)

    # claim visibility = 1s, heartbeat every 0.25s, CV blocks for 1.5s
    # (> visibility) -- exactly the shape required to distinguish "the
    # event loop stayed free" from "it didn't".
    monkeypatch.setattr(analysis_worker, "CLAIM_VISIBILITY_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(analysis_worker, "HEARTBEAT_INTERVAL_SECONDS", 0.25)
    monkeypatch.setattr(analysis_worker, "HEARTBEAT_EXTENSION_SECONDS", 5)

    job_queue_a = PostgresJobQueue(app_db_pool)
    job_queue_b = PostgresJobQueue(app_db_pool)  # simulates a second worker process
    execution_service, blocking_pipeline = _real_execution_service_with_blocking_cv(
        app_db_pool, fake_storage, delay_seconds=1.5,
    )

    worker_task = asyncio.create_task(analysis_worker.run_one_cycle(job_queue_a, execution_service))
    # Past the original 1s visibility window while the CV call is still
    # synchronously blocking its own (dedicated executor) thread -- the
    # only way this claim attempt from a second queue instance can find
    # nothing is if the heartbeat task, running on the *main* event
    # loop, was actually free to fire during that window.
    await asyncio.sleep(1.2)

    reclaimed = await job_queue_b.claim(analysis_worker.ANALYSIS_JOB_TYPE, visibility_timeout_seconds=1)
    assert reclaimed is None  # second worker could not reclaim: the loop stayed free to heartbeat

    claimed = await worker_task
    assert claimed is True

    # CV ran exactly once -- no duplicate compute despite worker B's
    # claim attempt.
    assert blocking_pipeline.analyze_calls == 1

    job = await db_pool.fetchrow("SELECT status FROM jobs WHERE request_id = $1", request_id)
    assert job["status"] == "completed"

    # Result persisted exactly once, quota consumed exactly once.
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "CONSUMED"
    measurement_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM analysis_measurements am "
        "JOIN analysis_requests ar ON ar.id = am.analysis_request_id "
        "WHERE ar.request_id = $1",
        request_id,
    )
    assert measurement_count > 0

    # Worker B never claimed anything and never has any duplicate-
    # completion work left to do.
    assert await job_queue_b.claim(analysis_worker.ANALYSIS_JOB_TYPE) is None
