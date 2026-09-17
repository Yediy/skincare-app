"""System Integrity Gate V1, section 2: execution-level lease fencing.

Queue-level fencing (tests/queue/test_postgres_job_queue.py) alone
cannot protect a worker that is already inside synchronous CV compute
when its queue lease expires and a second worker legitimately
reclaims the underlying job -- these tests drive
analysis_requests.processing_claim_token directly (via
app.db.analysis_repository.mark_processing/commit_analysis_result),
simulating exactly that interleaving without needing real concurrency:
worker A installs its token, worker B (a legitimate reclaim) installs
its own afterward, and A's own prior-captured token is proven unable
to commit a result, mark the request FAILED, release quota, or delete
the image out from under B -- while B's own operations, using its own
current token, proceed normally.
"""
import base64
import uuid
from pathlib import Path

import pytest

from app.db import analysis_repository
from app.domain.analysis_execution_service import (
    AnalysisExecutionLeaseLostError,
    AnalysisExecutionService,
)
from app.domain.analysis_service import compute_analysis
from app.domain.analysis_submission_service import AnalysisSubmissionService
from app.domain.entitlement import FreeTierEntitlementService, UsagePolicyService
from app.domain.product_matching_service import ProductMatchingService
from app.queue.postgres_queue import PostgresJobQueue
from app.storage.base import ObjectNotFoundError, ObjectStorage, ObjectStorageUnavailableError
from app.storage.ephemeral_image_store import EphemeralAnalysisImageStore

GRACE_HOPPER_JPG = Path(__file__).resolve().parent.parent / "fixtures" / "grace_hopper.jpg"


class FakeObjectStorage(ObjectStorage):
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def put(self, key, data, *, content_type=None):
        self.objects[key] = data

    async def get(self, key):
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]

    async def delete(self, key):
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


@pytest.fixture
def real_singletons():
    from app.main import pipeline, plan_service, safety_engine, scorer
    return {"pipeline": pipeline, "scorer": scorer, "plan_service": plan_service, "safety_engine": safety_engine}


async def _submit(app_db_pool, user_id, fake_storage, allowance=5):
    usage_policy_service = UsagePolicyService(app_db_pool, FreeTierEntitlementService(allowance))
    submission_service = AnalysisSubmissionService(
        app_db_pool,
        usage_policy_service=usage_policy_service,
        image_store=EphemeralAnalysisImageStore(fake_storage),
        job_queue=PostgresJobQueue(app_db_pool),
        async_image_storage_enabled=True,
    )
    request_id = str(uuid.uuid4())
    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode()
    submitted = await submission_service.submit(user_id, request_id, image_b64)
    return submitted, request_id, usage_policy_service


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


async def _compute_real_result(app_db_pool, user_id, req, fake_storage, real_singletons):
    image_bytes = await EphemeralAnalysisImageStore(fake_storage).retrieve(req["image_object_key"])
    return await compute_analysis(
        app_db_pool, user_id, image_bytes,
        pipeline=real_singletons["pipeline"], scorer=real_singletons["scorer"],
        plan_service=real_singletons["plan_service"],
        product_matching_service=ProductMatchingService(app_db_pool, real_singletons["safety_engine"]),
        safety_engine=real_singletons["safety_engine"],
    )


async def _commit(app_db_pool, user_id, analysis_request_id, req, result, *, processing_claim_token):
    await analysis_repository.commit_analysis_result(
        app_db_pool, user_id, analysis_request_id,
        capture_assessment=result.capture_assessment,
        scores=result.scores,
        plan=result.plan,
        eligible_for_longitudinal_comparison=result.eligible_for_longitudinal_comparison,
        pipeline_version=result.capture_assessment.get("capture_pipeline_version") or "unknown",
        metric_results=result.metric_results,
        product_recommendations=result.product_recommendations,
        usage_reservation_id=req["usage_reservation_id"],
        processing_claim_token=processing_claim_token,
    )


async def test_stale_commit_rejected_after_newer_worker_takes_ownership_then_newer_commits_normally(
    db_pool, app_db_pool, real_singletons,
):
    """The key cross-layer invariant (System Integrity Gate V1, section
    3's required test): A finishes its (already in-flight) compute
    AFTER B has reclaimed and installed its own token -- A's commit
    must be rejected and change nothing; B's own subsequent commit,
    using its own current token, must succeed normally."""
    user_id = await _create_user_with_consent(db_pool, "lease-commit-race@test.com")
    fake_storage = FakeObjectStorage()
    submitted, request_id, _ = await _submit(app_db_pool, user_id, fake_storage)

    token_a = uuid.uuid4()
    token_b = uuid.uuid4()

    # A begins processing.
    await analysis_repository.mark_processing(app_db_pool, user_id, submitted.analysis_request_id, token_a)
    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    result = await _compute_real_result(app_db_pool, user_id, req, fake_storage, real_singletons)

    # A's lease expires; B legitimately reclaims and installs its own
    # token while A's compute above is still (conceptually) in flight.
    await analysis_repository.mark_processing(app_db_pool, user_id, submitted.analysis_request_id, token_b)

    # A finishes compute first and tries to commit with its now-stale token.
    with pytest.raises(analysis_repository.AnalysisResultCommitFencedError):
        await _commit(app_db_pool, user_id, submitted.analysis_request_id, req, result, processing_claim_token=token_a)

    mid_state = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert mid_state["status"] == "PROCESSING"  # A's rejected commit changed nothing
    result_row = await analysis_repository.get_result(app_db_pool, user_id, submitted.analysis_request_id)
    assert result_row is None
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RESERVED"

    # B may commit normally using its own, current token.
    await _commit(app_db_pool, user_id, submitted.analysis_request_id, req, result, processing_claim_token=token_b)

    final_state = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert final_state["status"] == "COMPLETED"
    result_row = await analysis_repository.get_result(app_db_pool, user_id, submitted.analysis_request_id)
    assert result_row is not None
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "CONSUMED"


async def test_execute_translates_the_repository_fencing_error(db_pool, app_db_pool, real_singletons, monkeypatch):
    """Proves the service layer (not just the repository) surfaces
    this as AnalysisExecutionLeaseLostError -- the error type the
    worker actually catches -- rather than leaking the repository's
    own internal AnalysisResultCommitFencedError. Drives this through
    a real execute() call (token_a) by making the CV-compute step
    itself trigger B's reclaim as a side effect -- accurately modeling
    "B reclaims while A's compute is still running" without needing
    real concurrency."""
    import app.domain.analysis_execution_service as service_module

    user_id = await _create_user_with_consent(db_pool, "lease-service-translate@test.com")
    fake_storage = FakeObjectStorage()
    submitted, request_id, usage_policy_service = await _submit(app_db_pool, user_id, fake_storage)
    execution_service = _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons)

    token_a = uuid.uuid4()
    token_b = uuid.uuid4()
    real_compute_analysis = service_module.compute_analysis

    async def _compute_then_let_b_reclaim(*args, **kwargs):
        result = await real_compute_analysis(*args, **kwargs)
        await analysis_repository.mark_processing(app_db_pool, user_id, submitted.analysis_request_id, token_b)
        return result

    monkeypatch.setattr(service_module, "compute_analysis", _compute_then_let_b_reclaim)

    with pytest.raises(AnalysisExecutionLeaseLostError):
        await execution_service.execute(user_id, submitted.analysis_request_id, token_a)

    req_after = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req_after["status"] == "PROCESSING"  # A's rejected commit changed nothing
    assert req_after["processing_claim_token"] == token_b  # still B's


async def test_stale_worker_cannot_terminal_fail_after_newer_worker_completed(db_pool, app_db_pool, real_singletons):
    """A processing -> lease expires -> B takes ownership -> B
    completes -> A attempts terminal failure -> request remains
    COMPLETED, result remains present, quota remains CONSUMED -- A's
    attempt must have no effect on any of that."""
    user_id = await _create_user_with_consent(db_pool, "lease-terminal-after-complete@test.com")
    fake_storage = FakeObjectStorage()
    submitted, request_id, usage_policy_service = await _submit(app_db_pool, user_id, fake_storage)
    execution_service = _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons)

    token_a = uuid.uuid4()
    token_b = uuid.uuid4()

    await analysis_repository.mark_processing(app_db_pool, user_id, submitted.analysis_request_id, token_a)
    # B reclaims and runs its own full, legitimate execute() to completion.
    outcome = await execution_service.execute(user_id, submitted.analysis_request_id, token_b)
    assert outcome.already_completed is False

    # A, still holding its now-stale token, attempts terminal failure --
    # must be a no-op: the request is already terminal (COMPLETED), and
    # nothing about that must change.
    await execution_service.mark_terminal_failure(user_id, submitted.analysis_request_id, "SOME_ERROR", token_a)

    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["status"] == "COMPLETED"
    result_row = await analysis_repository.get_result(app_db_pool, user_id, submitted.analysis_request_id)
    assert result_row is not None
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "CONSUMED"


async def test_stale_worker_cannot_terminal_fail_while_newer_worker_still_processing(db_pool, app_db_pool):
    """A owns processing, its lease is (conceptually) lost, B installs
    its own token and is still actively processing (not yet complete)
    -- A's terminal-failure attempt with its stale token must be
    rejected (AnalysisExecutionLeaseLostError), releasing no quota and
    marking nothing FAILED, since B may still legitimately need both."""
    user_id = await _create_user_with_consent(db_pool, "lease-terminal-still-processing@test.com")
    fake_storage = FakeObjectStorage()
    submitted, request_id, usage_policy_service = await _submit(app_db_pool, user_id, fake_storage)

    from app.domain.entitlement import UsagePolicyService as _UPS
    execution_service = AnalysisExecutionService(
        app_db_pool,
        pipeline=None, scorer=None, plan_service=None,  # unused -- this test never calls execute()
        usage_policy_service=usage_policy_service,
        product_matching_service=None,
        safety_engine=None,
        image_store=EphemeralAnalysisImageStore(fake_storage),
    )

    token_a = uuid.uuid4()
    token_b = uuid.uuid4()

    await analysis_repository.mark_processing(app_db_pool, user_id, submitted.analysis_request_id, token_a)
    await analysis_repository.mark_processing(app_db_pool, user_id, submitted.analysis_request_id, token_b)

    with pytest.raises(AnalysisExecutionLeaseLostError):
        await execution_service.mark_terminal_failure(user_id, submitted.analysis_request_id, "SOME_ERROR", token_a)

    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["status"] == "PROCESSING"  # unchanged -- A's attempt was fenced out
    assert req["processing_claim_token"] == token_b  # still B's, untouched by A
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RESERVED"  # never released by A's rejected attempt


# --- Independent-review follow-up: PROCESSING ENTRY fencing --------------
#
# The races above all prove EXIT-side fencing (commit_analysis_result(),
# mark_failed()) correctly reject a stale token. Independent review
# found the ENTRY side (mark_processing() itself) was NOT fenced at
# all: it installed whatever token it was given unconditionally, so a
# stale worker A resuming after a legitimate worker B had already
# reclaimed the same queue job and installed its own ownership could
# simply overwrite B's processing_claim_token back to A's own stale
# one -- reversing legitimate ownership. These tests drive the REAL
# PostgresJobQueue (never two arbitrary UUIDs standing in for
# "ownership") to prove the fix: mark_processing() now proves, in the
# same statement as the install, that the presented (job_id,
# claim_token) pair is still the queue's current claim on a real
# `analysis` job that corresponds to this exact analysis request via
# the durable jobs.request_id == analysis_requests.request_id
# relationship.

async def test_processing_entry_ownership_cannot_be_stolen_back_by_stale_queue_claim(
    db_pool, app_db_pool, real_singletons,
):
    """The exact required regression sequence: A claims the real queue
    job and enters PROCESSING; A's lease expires; B legitimately
    reclaims the SAME job and installs its own ownership; stale A's
    later attempt to reinstall itself (same job_id, its own now-stale
    token) must be rejected without altering B's ownership; B can then
    continue and commit normally."""
    user_id = await _create_user_with_consent(db_pool, "entry-fencing-takeover@test.com")
    fake_storage = FakeObjectStorage()
    submitted, request_id, _ = await _submit(app_db_pool, user_id, fake_storage)
    job_queue = PostgresJobQueue(app_db_pool)

    claimed_a = await job_queue.claim("analysis", visibility_timeout_seconds=0)
    assert claimed_a is not None
    job_id = claimed_a.id
    token_a = claimed_a.claim_token

    installed_a = await analysis_repository.mark_processing(
        app_db_pool, user_id, submitted.analysis_request_id, token_a, job_id=job_id,
    )
    assert installed_a is True
    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["status"] == "PROCESSING"
    assert req["processing_claim_token"] == token_a

    # visibility_timeout_seconds=0 above already put A's lease in the
    # past -- this is a legitimate reclaim, not a race being exploited.
    claimed_b = await job_queue.claim("analysis")
    assert claimed_b is not None
    assert claimed_b.id == job_id
    token_b = claimed_b.claim_token
    assert token_b != token_a

    installed_b = await analysis_repository.mark_processing(
        app_db_pool, user_id, submitted.analysis_request_id, token_b, job_id=job_id,
    )
    assert installed_b is True
    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["processing_claim_token"] == token_b

    # Stale A resumes and tries to reinstall itself using the SAME
    # job_id and its own now-stale token.
    installed_a_again = await analysis_repository.mark_processing(
        app_db_pool, user_id, submitted.analysis_request_id, token_a, job_id=job_id,
    )
    assert installed_a_again is False

    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["processing_claim_token"] == token_b  # still B's -- A never reversed it
    assert req["status"] == "PROCESSING"

    # B continues and commits normally afterward.
    result = await _compute_real_result(app_db_pool, user_id, req, fake_storage, real_singletons)
    await _commit(app_db_pool, user_id, submitted.analysis_request_id, req, result, processing_claim_token=token_b)
    final = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert final["status"] == "COMPLETED"


async def test_stale_entry_rejected_even_before_newer_worker_enters_processing(db_pool, app_db_pool):
    """The narrower variant: B has reclaimed the queue job but hasn't
    called mark_processing() yet at all -- stale A's entry attempt must
    still be rejected purely because the *queue* already disagrees A
    holds the current claim, independent of whatever
    analysis_requests.processing_claim_token currently holds (nothing,
    in this case -- A itself never got to install it either)."""
    user_id = await _create_user_with_consent(db_pool, "entry-fencing-before-b-enters@test.com")
    fake_storage = FakeObjectStorage()
    submitted, request_id, _ = await _submit(app_db_pool, user_id, fake_storage)
    job_queue = PostgresJobQueue(app_db_pool)

    claimed_a = await job_queue.claim("analysis", visibility_timeout_seconds=0)
    job_id = claimed_a.id
    token_a = claimed_a.claim_token

    claimed_b = await job_queue.claim("analysis")  # legitimate reclaim
    token_b = claimed_b.claim_token
    assert token_b != token_a

    # B has NOT called mark_processing() yet -- the queue alone already
    # disagrees A owns the claim.
    installed_a = await analysis_repository.mark_processing(
        app_db_pool, user_id, submitted.analysis_request_id, token_a, job_id=job_id,
    )
    assert installed_a is False

    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["status"] == "QUEUED"  # A's rejected entry never touched status
    assert req["processing_claim_token"] is None


async def test_token_from_one_analysis_job_cannot_install_ownership_for_a_different_analysis_request(
    db_pool, app_db_pool,
):
    """A token that IS a real, currently-claimed `analysis` job's
    claim_token must still be rejected if that job doesn't correspond
    to the analysis request being entered -- proves the fix binds job
    identity to the SPECIFIC analysis request via the durable
    jobs.request_id == analysis_requests.request_id relationship, not
    merely "some currently claimed analysis job, any of them"."""
    user_id = await _create_user_with_consent(db_pool, "entry-fencing-mismatch@test.com")
    fake_storage = FakeObjectStorage()
    submitted_x, _, _ = await _submit(app_db_pool, user_id, fake_storage)
    submitted_y, _, _ = await _submit(app_db_pool, user_id, fake_storage)

    job_queue = PostgresJobQueue(app_db_pool)
    claimed = await job_queue.claim("analysis")
    assert claimed is not None

    # Whichever of X/Y this real job actually belongs to, prove the
    # OTHER one rejects it -- order-independent.
    if claimed.payload["analysis_request_id"] == str(submitted_x.analysis_request_id):
        mismatched_request_id = submitted_y.analysis_request_id
    else:
        mismatched_request_id = submitted_x.analysis_request_id

    installed = await analysis_repository.mark_processing(
        app_db_pool, user_id, mismatched_request_id, claimed.claim_token, job_id=claimed.id,
    )
    assert installed is False

    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, mismatched_request_id)
    assert req["status"] == "QUEUED"
    assert req["processing_claim_token"] is None


async def test_execute_surfaces_entry_lease_loss_without_touching_image_or_quota(
    db_pool, app_db_pool, real_singletons,
):
    """Service-level: the same stale-entry race driven through
    AnalysisExecutionService.execute() itself, not just
    mark_processing() directly -- proves execute() surfaces it as
    AnalysisExecutionLeaseLostError and never reaches image retrieval/
    compute/quota mutation once the ownership install itself has
    already failed."""
    user_id = await _create_user_with_consent(db_pool, "entry-fencing-service-level@test.com")
    fake_storage = FakeObjectStorage()
    submitted, request_id, usage_policy_service = await _submit(app_db_pool, user_id, fake_storage)
    execution_service = _execution_service(app_db_pool, fake_storage, usage_policy_service, real_singletons)
    job_queue = PostgresJobQueue(app_db_pool)

    claimed_a = await job_queue.claim("analysis", visibility_timeout_seconds=0)
    job_id = claimed_a.id
    token_a = claimed_a.claim_token

    claimed_b = await job_queue.claim("analysis")  # B legitimately reclaims
    token_b = claimed_b.claim_token

    # B installs its own ownership first, exactly as the real worker
    # flow would (via its own execute() call).
    installed_b = await analysis_repository.mark_processing(
        app_db_pool, user_id, submitted.analysis_request_id, token_b, job_id=job_id,
    )
    assert installed_b is True

    # Stale A now calls execute() itself, not mark_processing() directly.
    with pytest.raises(AnalysisExecutionLeaseLostError):
        await execution_service.execute(user_id, submitted.analysis_request_id, token_a, job_id)

    # Never touched the image: still exactly the one object
    # AnalysisSubmissionService itself uploaded at submission time --
    # execute() never reached retrieve()/compute for A's rejected attempt.
    assert len(fake_storage.objects) == 1

    # Quota untouched by A's rejected attempt.
    usage = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE request_id = $1", request_id)
    assert usage["status"] == "RESERVED"

    # analysis_requests state still belongs to B, untouched by A.
    req = await analysis_repository.get_request_by_id(app_db_pool, user_id, submitted.analysis_request_id)
    assert req["processing_claim_token"] == token_b
    assert req["status"] == "PROCESSING"
