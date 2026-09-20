"""app.domain.analysis_submission_service -- Part V continuation: the
async submission failure-saga hardening (create_request failure
releases quota; an R2-upload-then-DB-enqueue failure compensates
rather than orphaning the object/reservation), concurrent idempotent
submit semantics (real DB uniqueness, no process-local lock), strict
base64 validation, and (Mobile Phase B post-merge audit repair pass,
section 7) the server-side image-size/dimension limits that make this
endpoint independent of whatever the mobile client claims to have
resized.

Runs against the real restricted skincare_app role (app_db_pool) and a
real PostgresJobQueue -- nothing here mocks Postgres concurrency,
RLS, or job claiming; only the object-storage backend (a fake, exactly
like tests/storage/test_ephemeral_image_store.py's) and, for the two
failure-window tests, a deliberately-broken create_request/enqueue.
"""
import asyncio
import base64
import io
import uuid
from pathlib import Path

import pytest
from PIL import Image as PILImage

from app.db import analysis_repository
from app.domain.analysis_submission_service import (
    MAX_ANALYSIS_BASE64_CHARS,
    MAX_ANALYSIS_DECODED_BYTES,
    MAX_ANALYSIS_IMAGE_LONG_EDGE,
    MAX_ANALYSIS_IMAGE_PIXELS,
    AnalysisSubmissionService,
    AsyncImageStorageDisabledError,
    ConsentRequiredError,
    ImagePayloadTooLargeError,
    InvalidImageError,
)
from app.domain.entitlement import FreeTierEntitlementService, QuotaExceededError, UsagePolicyService
from app.queue.base import JobQueue
from app.queue.postgres_queue import PostgresJobQueue
from app.storage.base import ObjectNotFoundError, ObjectStorage, ObjectStorageUnavailableError
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore

GRACE_HOPPER_JPG = Path(__file__).resolve().parent.parent / "fixtures" / "grace_hopper.jpg"
# A real, decodable 512x600 JPEG -- unlike the old fixture below, this
# must survive the section-7 Pillow header check, since every
# successful-submission test in this file uses it.
VALID_IMAGE_B64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
# Valid base64 (decodes cleanly) but not any recognizable image format
# at all -- exercises the base64-is-fine/content-is-not path,
# distinctly from INVALID_IMAGE_B64 (bad base64 syntax) below.
NOT_AN_IMAGE_B64 = base64.b64encode(b"fake-jpeg-bytes-not-really-an-image").decode()
INVALID_IMAGE_B64 = "!!!not-valid-base64$$$"


def _jpeg_b64(width: int, height: int) -> str:
    """A real, minimal JPEG of exactly the given dimensions -- for
    exercising the section-7C dimension/pixel-count bound without
    needing a checked-in oversized fixture file."""
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), color=(128, 128, 128)).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


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


# Section 7: server-side image input limits. "The mobile client is not
# a security boundary" -- these exercise the service independently of
# any HTTP layer, mirroring the failure-mode style of the base64-
# validation tests above.

async def test_oversized_encoded_payload_is_rejected_before_decoding_and_releases_quota(db_pool, app_db_pool):
    user_id = await _create_user_with_consent(db_pool, "sub-b64toolong@test.com")
    service = _service(app_db_pool)
    request_id = str(uuid.uuid4())
    oversized_b64 = "A" * (MAX_ANALYSIS_BASE64_CHARS + 1)

    with pytest.raises(ImagePayloadTooLargeError):
        await service.submit(user_id, request_id, oversized_b64)

    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RELEASED"
    req = await analysis_repository.get_request_by_request_id(app_db_pool, user_id, request_id)
    assert req is None  # create_request() must never have been reached


async def test_oversized_decoded_payload_is_rejected_and_releases_quota(db_pool, app_db_pool):
    """A payload whose DECODED byte count exceeds
    MAX_ANALYSIS_DECODED_BYTES must be rejected as too large. Given
    these particular constants (MAX_ANALYSIS_BASE64_CHARS == exactly
    4/3 * MAX_ANALYSIS_DECODED_BYTES), any such payload's *encoded*
    length also exceeds MAX_ANALYSIS_BASE64_CHARS -- base64 can never
    decode to more bytes than 3/4 of its own encoded length -- so this
    is in practice caught by the encoded-length pre-filter before the
    dedicated post-decode byte-count check ever runs. Both are real,
    independently-implemented layers (defense in depth: the pre-filter
    is a cheap string-length check with no decode step, the post-decode
    check is the authoritative one against the actual bytes); this test
    asserts the resulting behavior -- rejected as too large, quota
    released -- which holds regardless of which layer fires first."""
    user_id = await _create_user_with_consent(db_pool, "sub-decodedtoolong@test.com")
    service = _service(app_db_pool)
    request_id = str(uuid.uuid4())
    oversized_decoded_b64 = base64.b64encode(b"\x00" * (MAX_ANALYSIS_DECODED_BYTES + 1)).decode()

    with pytest.raises(ImagePayloadTooLargeError):
        await service.submit(user_id, request_id, oversized_decoded_b64)

    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RELEASED"


async def test_excessive_dimensions_are_rejected_and_release_quota(db_pool, app_db_pool):
    user_id = await _create_user_with_consent(db_pool, "sub-toowide@test.com")
    service = _service(app_db_pool)
    request_id = str(uuid.uuid4())
    too_wide_b64 = _jpeg_b64(MAX_ANALYSIS_IMAGE_LONG_EDGE + 1, 100)

    with pytest.raises(InvalidImageError):
        await service.submit(user_id, request_id, too_wide_b64)

    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RELEASED"


async def test_pixel_count_at_exactly_the_bound_is_accepted_not_rejected(db_pool, app_db_pool):
    """A square image exactly at both MAX_ANALYSIS_IMAGE_LONG_EDGE and
    MAX_ANALYSIS_IMAGE_PIXELS (2048x2048 == 4,194,304px, by
    construction of these two constants) must be accepted -- the
    pixel-count check is a real, independently-evaluated bound (not
    merely a by-product of the dimension check), and its boundary is
    "greater than", never "greater than or equal to"."""
    assert MAX_ANALYSIS_IMAGE_LONG_EDGE * MAX_ANALYSIS_IMAGE_LONG_EDGE == MAX_ANALYSIS_IMAGE_PIXELS
    user_id = await _create_user_with_consent(db_pool, "sub-exactlimit@test.com")
    service = _service(app_db_pool)
    request_id = str(uuid.uuid4())
    at_limit_b64 = _jpeg_b64(MAX_ANALYSIS_IMAGE_LONG_EDGE, MAX_ANALYSIS_IMAGE_LONG_EDGE)

    result = await service.submit(user_id, request_id, at_limit_b64)

    assert result.status == "QUEUED"


async def test_malformed_image_bytes_that_are_valid_base64_are_rejected(db_pool, app_db_pool):
    """Distinct from INVALID_IMAGE_B64 (bad base64 syntax): this base64
    decodes cleanly, but the resulting bytes are not any recognizable
    image format at all."""
    user_id = await _create_user_with_consent(db_pool, "sub-notanimage@test.com")
    service = _service(app_db_pool)
    request_id = str(uuid.uuid4())

    with pytest.raises(InvalidImageError):
        await service.submit(user_id, request_id, NOT_AN_IMAGE_B64)

    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RELEASED"


async def test_ordinary_1600px_jpeg_is_accepted(db_pool, app_db_pool):
    """The official mobile client's own resize bound -- comfortably
    inside every section-7 server-side limit -- must never be
    rejected."""
    user_id = await _create_user_with_consent(db_pool, "sub-1600ok@test.com")
    service = _service(app_db_pool)
    request_id = str(uuid.uuid4())
    normal_b64 = _jpeg_b64(1600, 1200)

    result = await service.submit(user_id, request_id, normal_b64)

    assert result.status == "QUEUED"


async def test_same_request_id_is_independent_across_users(db_pool, app_db_pool):
    """Client request IDs are per-account idempotency keys, not global
    names that one account can squat to interfere with another."""
    user_a = await _create_user_with_consent(db_pool, "sub-scope-a@test.com")
    user_b = await _create_user_with_consent(db_pool, "sub-scope-b@test.com")
    request_id = str(uuid.uuid4())

    usage_a = UsagePolicyService(app_db_pool, FreeTierEntitlementService(monthly_allowance=5))
    usage_b = UsagePolicyService(app_db_pool, FreeTierEntitlementService(monthly_allowance=5))
    image_store = FakeImageStore()
    queue = PostgresJobQueue(app_db_pool)

    service_a = AnalysisSubmissionService(
        app_db_pool, usage_policy_service=usage_a, image_store=image_store,
        job_queue=queue, async_image_storage_enabled=True,
    )
    service_b = AnalysisSubmissionService(
        app_db_pool, usage_policy_service=usage_b, image_store=image_store,
        job_queue=queue, async_image_storage_enabled=True,
    )

    # Reuse the test suite's known-good small image fixture/helper input.
    image_base64 = _valid_image_base64()
    a = await service_a.submit(user_a, request_id, image_base64)
    b = await service_b.submit(user_b, request_id, image_base64)

    assert a.analysis_request_id != b.analysis_request_id
    assert a.replay is False
    assert b.replay is False
    assert await db_pool.fetchval(
        "SELECT COUNT(*) FROM analysis_requests WHERE request_id = $1", request_id
    ) == 2
    assert await db_pool.fetchval(
        "SELECT COUNT(*) FROM jobs WHERE job_type = 'analysis' AND request_id = $1", request_id
    ) == 2
