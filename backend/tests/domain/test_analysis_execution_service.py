"""app.domain.analysis_execution_service.AnalysisExecutionService --
Part VI: executes one already-submitted durable analysis request,
reusing the reservation AnalysisSubmissionService created at
submission (never reserving a second one), commits atomically, and
manages raw-image cleanup. Called directly here (not through the
worker/job queue -- that's tests/workers/test_analysis_worker.py's
job) with real Postgres and a fake object-storage backend.
"""
import base64
import io
import uuid
from pathlib import Path

import pytest
from PIL import Image

from app.db import analysis_repository
from app.domain.analysis_execution_service import (
    AnalysisExecutionService,
    AnalysisRequestInvalidStateError,
    AnalysisRequestNotFoundError,
    ImageRetrievalError,
)
from app.domain.analysis_submission_service import AnalysisSubmissionService
from app.domain.entitlement import FreeTierEntitlementService, UsagePolicyService
from app.domain.product_matching_service import ProductMatchingService
from app.cv.pipeline import NoFaceDetectedError
from app.queue.postgres_queue import PostgresJobQueue
from app.storage.base import ObjectNotFoundError, ObjectStorage, ObjectStorageUnavailableError
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore

GRACE_HOPPER_JPG = Path(__file__).resolve().parent.parent / "fixtures" / "grace_hopper.jpg"


class FakeObjectStorage(ObjectStorage):
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.unavailable = False
        self.delete_calls = 0

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
        self.delete_calls += 1
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


@pytest.fixture
def real_singletons():
    from app.main import pipeline, plan_service, safety_engine, scorer
    return {"pipeline": pipeline, "scorer": scorer, "plan_service": plan_service, "safety_engine": safety_engine}


def _submission_service(app_db_pool, fake_storage, allowance=5):
    usage_policy_service = UsagePolicyService(app_db_pool, FreeTierEntitlementService(allowance))
    return AnalysisSubmissionService(
        app_db_pool,
        usage_policy_service=usage_policy_service,
        image_store=EphemeralAnalysisImageStore(fake_storage),
        job_queue=PostgresJobQueue(app_db_pool),
        async_image_storage_enabled=True,
    ), usage_policy_service


def _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons):
    safety_engine = real_singletons["safety_engine"]
    return AnalysisExecutionService(
        app_db_pool,
        pipeline=real_singletons["pipeline"],
        scorer=real_singletons["scorer"],
        plan_service=real_singletons["plan_service"],
        usage_policy_service=usage_policy_service,
        product_matching_service=ProductMatchingService(app_db_pool, safety_engine),
        safety_engine=safety_engine,
        image_store=EphemeralAnalysisImageStore(fake_storage),
    )


async def test_execute_not_found_raises(app_db_pool, real_singletons):
    fake_storage = FakeObjectStorage()
    usage_policy_service = UsagePolicyService(app_db_pool, FreeTierEntitlementService())
    execution_service = _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons)
    with pytest.raises(AnalysisRequestNotFoundError):
        await execution_service.execute(uuid.uuid4(), uuid.uuid4())


async def test_execute_on_received_request_raises_invalid_state(db_pool, app_db_pool, real_singletons):
    """A request that was never actually QUEUED (still RECEIVED, no
    image reference) is not executable -- a data/state bug, not
    something retrying would fix."""
    user_id = await _create_user_with_consent(db_pool, "exec-invalidstate@test.com")
    req = await analysis_repository.create_request(app_db_pool, user_id, str(uuid.uuid4()))
    fake_storage = FakeObjectStorage()
    usage_policy_service = UsagePolicyService(app_db_pool, FreeTierEntitlementService())
    execution_service = _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons)

    with pytest.raises(AnalysisRequestInvalidStateError):
        await execution_service.execute(user_id, req["id"])


async def test_successful_execution_commits_consumes_quota_and_deletes_image(db_pool, app_db_pool, real_singletons):
    user_id = await _create_user_with_consent(db_pool, "exec-happy@test.com")
    fake_storage = FakeObjectStorage()
    submission_service, usage_policy_service = _submission_service(app_db_pool, fake_storage)
    execution_service = _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons)

    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
    request_id = str(uuid.uuid4())
    submitted = await submission_service.submit(user_id, request_id, image_b64)
    assert submitted.status == "QUEUED"

    outcome = await execution_service.execute(user_id, submitted.analysis_request_id)
    assert outcome.already_completed is False

    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["status"] == "COMPLETED"
    assert req["image_object_key"] is None  # cleared after confirmed deletion

    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "CONSUMED"

    result = await analysis_repository.get_result(app_db_pool, user_id, submitted.analysis_request_id)
    assert result is not None

    # Raw image actually deleted, not merely dereferenced.
    assert len(fake_storage.objects) == 0


async def test_execute_on_already_completed_request_does_not_recompute(db_pool, app_db_pool, real_singletons):
    """Worker-crash-after-commit replay: calling execute() again for
    an already-COMPLETED request must do nothing further -- no second
    compute, no second quota consumption, no error."""
    user_id = await _create_user_with_consent(db_pool, "exec-replay@test.com")
    fake_storage = FakeObjectStorage()
    submission_service, usage_policy_service = _submission_service(app_db_pool, fake_storage)
    execution_service = _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons)

    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
    request_id = str(uuid.uuid4())
    submitted = await submission_service.submit(user_id, request_id, image_b64)
    first = await execution_service.execute(user_id, submitted.analysis_request_id)
    assert first.already_completed is False

    second = await execution_service.execute(user_id, submitted.analysis_request_id)
    assert second.already_completed is True

    measurement_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM analysis_measurements WHERE analysis_request_id = $1", submitted.analysis_request_id
    )
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "CONSUMED"  # still consumed exactly once, not double-consumed or reverted
    assert measurement_count > 0


async def test_execute_propagates_no_face_detected_for_worker_classification(db_pool, app_db_pool, real_singletons):
    """A capture with no detectable face must propagate
    NoFaceDetectedError unchanged -- classification into a terminal
    failure is app/workers/analysis_worker.py's job, not this
    service's; this service must never swallow it."""
    user_id = await _create_user_with_consent(db_pool, "exec-noface@test.com")
    fake_storage = FakeObjectStorage()
    submission_service, usage_policy_service = _submission_service(app_db_pool, fake_storage)
    execution_service = _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons)

    request_id = str(uuid.uuid4())
    submitted = await submission_service.submit(user_id, request_id, _blank_image_b64())

    with pytest.raises(NoFaceDetectedError):
        await execution_service.execute(user_id, submitted.analysis_request_id)

    # Nothing committed -- the request is not COMPLETED, and the
    # reservation is untouched by this service (the worker's job to
    # release it on a terminal outcome).
    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["status"] != "COMPLETED"
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RESERVED"


async def test_image_retrieval_failure_wraps_original_exception(db_pool, app_db_pool, real_singletons):
    user_id = await _create_user_with_consent(db_pool, "exec-imgfail@test.com")
    fake_storage = FakeObjectStorage()
    submission_service, usage_policy_service = _submission_service(app_db_pool, fake_storage)
    execution_service = _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons)

    request_id = str(uuid.uuid4())
    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
    submitted = await submission_service.submit(user_id, request_id, image_b64)

    fake_storage.unavailable = True
    with pytest.raises(ImageRetrievalError) as exc_info:
        await execution_service.execute(user_id, submitted.analysis_request_id)
    assert isinstance(exc_info.value.__cause__, ObjectStorageUnavailableError)


async def test_mark_terminal_failure_releases_quota_and_marks_failed(db_pool, app_db_pool, real_singletons):
    user_id = await _create_user_with_consent(db_pool, "exec-terminal@test.com")
    fake_storage = FakeObjectStorage()
    submission_service, usage_policy_service = _submission_service(app_db_pool, fake_storage)
    execution_service = _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons)

    request_id = str(uuid.uuid4())
    submitted = await submission_service.submit(user_id, request_id, _blank_image_b64())

    await execution_service.mark_terminal_failure(user_id, submitted.analysis_request_id, "NO_FACE_DETECTED")

    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["status"] == "FAILED"
    assert req["error_code"] == "NO_FACE_DETECTED"
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RELEASED"


async def test_mark_terminal_failure_survives_delete_failure(db_pool, app_db_pool, real_singletons):
    """The image object couldn't be deleted (storage unavailable) --
    the request must still be marked FAILED and quota still released;
    the DB-side image reference is left intact for the cleanup
    sweeper, never silently dropped."""
    user_id = await _create_user_with_consent(db_pool, "exec-terminal-delfail@test.com")
    fake_storage = FakeObjectStorage()
    submission_service, usage_policy_service = _submission_service(app_db_pool, fake_storage)
    execution_service = _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons)

    request_id = str(uuid.uuid4())
    submitted = await submission_service.submit(user_id, request_id, _blank_image_b64())

    fake_storage.unavailable = True
    await execution_service.mark_terminal_failure(user_id, submitted.analysis_request_id, "NO_FACE_DETECTED")

    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["status"] == "FAILED"
    assert req["image_object_key"] is not None  # left for the sweeper
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RELEASED"
