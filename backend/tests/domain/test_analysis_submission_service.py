"""app.domain.analysis_submission_service -- Part V continuation: the
async submission failure-saga hardening (create_request failure
releases quota; an R2-upload-then-DB-enqueue failure compensates
rather than orphaning the object/reservation), concurrent idempotent
submit semantics (real DB uniqueness, no process-local lock), and
strict base64 validation.

Runs against the real restricted skincare_app role (app_db_pool) and a
real PostgresJobQueue -- nothing here mocks Postgres concurrency,
RLS, or job claiming; only the object-storage backend (a fake, exactly
like tests/storage/test_ephemeral_image_store.py's) and, for the two
failure-window tests, a deliberately-broken create_request/enqueue.
"""
import asyncio
import base64
import uuid

import pytest

from app.db import analysis_repository
from app.domain.analysis_submission_service import (
    AnalysisSubmissionService,
    AsyncImageStorageDisabledError,
    ConsentRequiredError,
    InvalidImageError,
)
from app.domain.entitlement import FreeTierEntitlementService, QuotaExceededError, UsagePolicyService
from app.queue.base import JobQueue
from app.queue.postgres_queue import PostgresJobQueue
from app.storage.base import ObjectNotFoundError, ObjectStorage, ObjectStorageUnavailableError
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore

VALID_IMAGE_B64 = base64.b64encode(b"fake-jpeg-bytes-not-really-an-image").decode()
INVALID_IMAGE_B64 = "!!!not-valid-base64$$$"


class FakeObjectStorage(ObjectStorage):
    """Same shape as tests/storage/test_ephemeral_image_store.py's own
    fake -- duplicated locally rather than imported, since that module
    is a test file, not a fixture library."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.unavailable = False
        self.delete_calls = 0

    async def put(self, key, data, *, content_type=None):
        if self.unavailable:
            raise ObjectStorageUnavailableError("fake unavailable")
        self.objects[key] = data

    async def get(self, key):
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
        raise NotImplementedError("EphemeralAnalysisImageStore never calls this")

    async def create_download_authorization(self, key, *, expires_in_seconds=900):
        raise NotImplementedError("EphemeralAnalysisImageStore never calls this")


class _EnqueueFailingJobQueue(JobQueue):
    """Wraps a real PostgresJobQueue but always fails enqueue() -- for
    exercising failure window B (upload succeeds, the atomic
    mark_queued()+enqueue() transaction does not)."""

    def __init__(self, inner: JobQueue):
        self._inner = inner

    async def enqueue(self, *args, **kwargs):
        raise RuntimeError("simulated enqueue failure")

    async def claim(self, *args, **kwargs):
        return await self._inner.claim(*args, **kwargs)

    async def acknowledge(self, *args, **kwargs):
        return await self._inner.acknowledge(*args, **kwargs)

    async def fail(self, *args, **kwargs):
        return await self._inner.fail(*args, **kwargs)

    async def extend_visibility(self, *args, **kwargs):
        return await self._inner.extend_visibility(*args, **kwargs)


async def _create_user(db_pool, email: str):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


async def _create_user_with_consent(db_pool, email: str):
    from app.db.consent_repository import REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION, record_consent

    user_id = await _create_user(db_pool, email)
    await record_consent(db_pool, user_id, REQUIRED_CONSENT_TYPE, REQUIRED_POLICY_VERSION, "testing")
    return user_id


def _service(app_db_pool, *, image_store=None, job_queue=None, allowance=5, enabled=True):
    usage_policy_service = UsagePolicyService(app_db_pool, FreeTierEntitlementService(allowance))
    image_store = image_store if image_store is not None else EphemeralAnalysisImageStore(FakeObjectStorage())
    job_queue = job_queue if job_queue is not None else PostgresJobQueue(app_db_pool)
    return AnalysisSubmissionService(
        app_db_pool,
        usage_policy_service=usage_policy_service,
        image_store=image_store,
        job_queue=job_queue,
        async_image_storage_enabled=enabled,
    )


async def test_disabled_flag_refuses_to_enqueue(db_pool, app_db_pool):
    user_id = await _create_user_with_consent(db_pool, "sub-disabled@test.com")
    service = _service(app_db_pool, enabled=False)
    with pytest.raises(AsyncImageStorageDisabledError):
        await service.submit(user_id, str(uuid.uuid4()), VALID_IMAGE_B64)


async def test_missing_consent_raises(db_pool, app_db_pool):
    user_id = await _create_user(db_pool, "sub-noconsent@test.com")
    service = _service(app_db_pool)
    with pytest.raises(ConsentRequiredError):
        await service.submit(user_id, str(uuid.uuid4()), VALID_IMAGE_B64)


async def test_successful_submission_queues_request_and_job(db_pool, app_db_pool):
    user_id = await _create_user_with_consent(db_pool, "sub-happy@test.com")
    service = _service(app_db_pool)
    request_id = str(uuid.uuid4())

    result = await service.submit(user_id, request_id, VALID_IMAGE_B64)

    assert result.status == "QUEUED"
    assert result.replay is False
    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, result.analysis_request_id)
    assert req["status"] == "QUEUED"
    assert req["image_object_key"] is not None
    job = await db_pool.fetchrow("SELECT status FROM jobs WHERE request_id = $1", request_id)
    assert job["status"] == "pending"
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RESERVED"


async def test_sequential_replay_returns_existing_analysis(db_pool, app_db_pool):
    user_id = await _create_user_with_consent(db_pool, "sub-replay@test.com")
    service = _service(app_db_pool)
    request_id = str(uuid.uuid4())

    first = await service.submit(user_id, request_id, VALID_IMAGE_B64)
    second = await service.submit(user_id, request_id, VALID_IMAGE_B64)

    assert second.replay is True
    assert second.analysis_request_id == first.analysis_request_id
    job_count = await db_pool.fetchval("SELECT COUNT(*) FROM jobs WHERE request_id = $1", request_id)
    assert job_count == 1
    usage_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM analysis_usage WHERE request_id = $1", request_id
    )
    assert usage_count == 1


async def test_invalid_base64_is_rejected_strictly_and_releases_quota(db_pool, app_db_pool):
    """`validate=True` -- a payload with characters outside the base64
    alphabet must fail closed (InvalidImageError), not be silently
    stripped down to whatever valid characters remain, and the
    reservation it consumed a slot for must be released, not stranded."""
    user_id = await _create_user_with_consent(db_pool, "sub-badb64@test.com")
    service = _service(app_db_pool)
    request_id = str(uuid.uuid4())

    with pytest.raises(InvalidImageError):
        await service.submit(user_id, request_id, INVALID_IMAGE_B64)

    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RELEASED"
    req = await analysis_repository.get_request_by_request_id(app_db_pool, user_id, request_id)
    assert req is None  # create_request() must never have been reached


async def test_create_request_failure_releases_quota(db_pool, app_db_pool, monkeypatch):
    """Failure window A: create_request() raising after
    reserve_analysis() succeeded must release the reservation rather
    than stranding it RESERVED forever."""
    user_id = await _create_user_with_consent(db_pool, "sub-createfail@test.com")
    service = _service(app_db_pool)
    request_id = str(uuid.uuid4())

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated create_request failure")

    monkeypatch.setattr(analysis_repository, "create_request", _boom)

    with pytest.raises(RuntimeError, match="simulated create_request failure"):
        await service.submit(user_id, request_id, VALID_IMAGE_B64)

    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RELEASED"


async def test_enqueue_failure_after_upload_compensates(db_pool, app_db_pool):
    """Failure window B: the image uploads successfully, but the
    atomic mark_queued()+enqueue() transaction fails. Compensation
    must: attempt to delete the just-uploaded object, release the
    reservation, and mark the request FAILED -- and the request must
    never be left QUEUED with no corresponding job."""
    user_id = await _create_user_with_consent(db_pool, "sub-enqueuefail@test.com")
    fake_storage = FakeObjectStorage()
    real_queue = PostgresJobQueue(app_db_pool)
    service = _service(
        app_db_pool,
        image_store=EphemeralAnalysisImageStore(fake_storage),
        job_queue=_EnqueueFailingJobQueue(real_queue),
    )
    request_id = str(uuid.uuid4())

    with pytest.raises(RuntimeError, match="simulated enqueue failure"):
        await service.submit(user_id, request_id, VALID_IMAGE_B64)

    req = await analysis_repository.get_request_by_request_id(app_db_pool, user_id, request_id)
    assert req is not None
    assert req["status"] == "FAILED"
    assert req["status"] != "QUEUED"

    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RELEASED"

    job_count = await db_pool.fetchval("SELECT COUNT(*) FROM jobs WHERE request_id = $1", request_id)
    assert job_count == 0

    # The upload succeeded, and delete_if_confirmed() was reachable --
    # the object must actually have been deleted, not merely attempted.
    assert len(fake_storage.objects) == 0
    assert fake_storage.delete_calls == 1


async def test_enqueue_failure_with_unreachable_storage_leaves_recoverable_reference(db_pool, app_db_pool):
    """Same failure window, but object-storage deletion itself is
    unavailable at compensation time: the image reference must be
    preserved on the request row (independent of the failed
    transaction) so the cleanup sweeper can recover it later, and the
    failure must not be silently swallowed."""
    user_id = await _create_user_with_consent(db_pool, "sub-enqueuefail-storagedown@test.com")
    fake_storage = FakeObjectStorage()
    real_queue = PostgresJobQueue(app_db_pool)
    service = _service(
        app_db_pool,
        image_store=EphemeralAnalysisImageStore(fake_storage),
        job_queue=_EnqueueFailingJobQueue(real_queue),
    )
    request_id = str(uuid.uuid4())

    async def _store_then_break(image_bytes, *, region="global"):
        stored = await EphemeralAnalysisImageStore(fake_storage).store(image_bytes, region=region)
        fake_storage.unavailable = True  # storage goes down *after* the successful upload
        return stored

    # Patch just this instance's store() to flip storage unavailable
    # right after a successful upload, so delete_if_confirmed() inside
    # compensation genuinely cannot reach the backend.
    service._image_store.store = _store_then_break

    with pytest.raises(RuntimeError, match="simulated enqueue failure"):
        await service.submit(user_id, request_id, VALID_IMAGE_B64)

    req = await analysis_repository.get_request_by_request_id(app_db_pool, user_id, request_id)
    assert req is not None
    assert req["status"] == "FAILED"
    # The object was never actually deleted (storage was unavailable)
    # -- the DB reference must still point at it for the sweeper.
    assert req["image_object_key"] is not None
    assert req["image_expires_at"] is not None
    assert len(fake_storage.objects) == 1  # genuinely still there, not lost track of

    overdue_now = await analysis_repository.find_overdue_ephemeral_images(app_db_pool, batch_limit=100)
    # Not yet overdue (expires_at is ~1 hour out) -- but the reference
    # exists and is well-formed for the sweeper to eventually pick up.
    assert all(row["id"] != req["id"] for row in overdue_now)


async def test_concurrent_first_submissions_same_request_id_resolve_to_one_analysis(db_pool, app_db_pool):
    """10 truly concurrent first-time submissions for the same
    (user, request_id): exactly one analysis_request, one usage
    reservation, one queue job -- and every caller, winner or not,
    must get back a SubmissionResult referencing that same logical
    analysis, not an error. Enforced purely by Postgres uniqueness/
    advisory-lock semantics (usage_repository.reserve(),
    analysis_requests.request_id UNIQUE, jobs (job_type, request_id)
    UNIQUE) -- no process-local lock anywhere in this test or the
    service under test."""
    user_id = await _create_user_with_consent(db_pool, "sub-concurrent@test.com")
    request_id = str(uuid.uuid4())
    services = [_service(app_db_pool, allowance=20) for _ in range(10)]

    results = await asyncio.gather(*[
        s.submit(user_id, request_id, VALID_IMAGE_B64) for s in services
    ])

    analysis_ids = {str(r.analysis_request_id) for r in results}
    assert analysis_ids == {str(results[0].analysis_request_id)}

    request_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM analysis_requests WHERE request_id = $1", request_id
    )
    assert request_count == 1
    usage_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM analysis_usage WHERE request_id = $1", request_id
    )
    assert usage_count == 1
    job_count = await db_pool.fetchval("SELECT COUNT(*) FROM jobs WHERE request_id = $1", request_id)
    assert job_count == 1
