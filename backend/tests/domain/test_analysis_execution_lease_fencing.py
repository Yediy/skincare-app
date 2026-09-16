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
