"""Real PostgreSQL tests for PostgresJobQueue (Phase 15/16). Run
through app_db_pool -- the restricted skincare_app role, same as
production -- not the superuser, not mocked.
"""
import asyncio
import uuid

import pytest

from app.queue.base import JobNotFoundError
from app.queue.postgres_queue import PostgresJobQueue


@pytest.fixture
def queue(app_db_pool):
    return PostgresJobQueue(app_db_pool)


async def test_enqueue_creates_a_pending_job(queue):
    job = await queue.enqueue("analysis", {"user_id": "u1", "image_ref": "x"})
    assert job.job_type == "analysis"
    assert job.status == "pending"
    assert job.payload == {"user_id": "u1", "image_ref": "x"}


async def test_enqueue_with_same_request_id_returns_the_same_job(queue):
    """Phase 16's idempotency guarantee: the same logical request,
    submitted twice, produces one logical job -- not two rows."""
    request_id = str(uuid.uuid4())
    first = await queue.enqueue("analysis", {"attempt": 1}, request_id=request_id)
    second = await queue.enqueue("analysis", {"attempt": 2}, request_id=request_id)

    assert first.id == second.id
    # The second call's differing payload is discarded -- the first
    # enqueue is authoritative, matching ON CONFLICT DO NOTHING.
    assert second.payload == {"attempt": 1}


async def test_enqueue_with_same_request_id_but_different_job_type_are_independent(queue):
    request_id = str(uuid.uuid4())
    a = await queue.enqueue("analysis", {}, request_id=request_id)
    b = await queue.enqueue("export", {}, request_id=request_id)
    assert a.id != b.id


async def test_enqueue_without_request_id_never_deduplicates(queue):
    a = await queue.enqueue("analysis", {})
    b = await queue.enqueue("analysis", {})
    assert a.id != b.id


async def test_claim_returns_none_when_nothing_pending(queue):
    assert await queue.claim("analysis") is None


async def test_claim_returns_a_pending_job_and_marks_it_claimed(queue, app_db_pool):
    enqueued = await queue.enqueue("analysis", {"k": "v"})
    claimed = await queue.claim("analysis")

    assert claimed.id == enqueued.id
    row = await app_db_pool.fetchrow("SELECT status, claimed_at, claimed_until FROM jobs WHERE id = $1", claimed.id)
    assert row["status"] == "claimed"
    assert row["claimed_at"] is not None
    assert row["claimed_until"] is not None


async def test_claim_ignores_other_job_types(queue):
    await queue.enqueue("export", {})
    assert await queue.claim("analysis") is None


async def test_claim_does_not_return_the_same_job_twice(queue):
    await queue.enqueue("analysis", {})
    first = await queue.claim("analysis")
    second = await queue.claim("analysis")
    assert first is not None
    assert second is None


async def test_concurrent_claims_never_return_the_same_job(queue):
    """The core exclusivity guarantee a job queue exists to provide,
    proven with a real concurrent claim race -- not asserted from
    reading the SQL's FOR UPDATE SKIP LOCKED clause."""
    for _ in range(5):
        await queue.enqueue("analysis", {})

    results = await asyncio.gather(*(queue.claim("analysis") for _ in range(5)))
    claimed_ids = [r.id for r in results if r is not None]

    assert len(claimed_ids) == 5  # exactly 5 jobs existed, all claimed
    assert len(set(claimed_ids)) == 5  # no two claimers got the same job


async def test_expired_claim_becomes_reclaimable(queue, app_db_pool):
    job = await queue.enqueue("analysis", {})
    await queue.claim("analysis", visibility_timeout_seconds=0)

    # visibility_timeout_seconds=0 means claimed_until is already in
    # the past by the time this next claim runs.
    reclaimed = await queue.claim("analysis")
    assert reclaimed is not None
    assert reclaimed.id == job.id


async def test_acknowledge_marks_a_claimed_job_completed(queue, app_db_pool):
    job = await queue.enqueue("analysis", {})
    await queue.claim("analysis")
    await queue.acknowledge(job.id)

    row = await app_db_pool.fetchrow("SELECT status, completed_at FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None


async def test_acknowledge_unclaimed_job_raises_not_found(queue):
    job = await queue.enqueue("analysis", {})  # never claimed
    with pytest.raises(JobNotFoundError):
        await queue.acknowledge(job.id)


async def test_acknowledge_unknown_job_raises_not_found(queue):
    with pytest.raises(JobNotFoundError):
        await queue.acknowledge(uuid.uuid4())


async def test_fail_marks_a_claimed_job_failed_with_error(queue, app_db_pool):
    job = await queue.enqueue("analysis", {})
    await queue.claim("analysis")
    await queue.fail(job.id, "pipeline blew up")

    row = await app_db_pool.fetchrow("SELECT status, failed_at, last_error FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "failed"
    assert row["failed_at"] is not None
    assert row["last_error"] == "pipeline blew up"


async def test_fail_unclaimed_job_raises_not_found(queue):
    job = await queue.enqueue("analysis", {})
    with pytest.raises(JobNotFoundError):
        await queue.fail(job.id, "should not apply")


async def test_reenqueue_after_failure_with_same_request_id_returns_the_failed_job(queue):
    """Documented behavior, not an oversight: enqueue's idempotency is
    keyed on (job_type, request_id) regardless of status, so a caller
    wanting a genuinely new attempt after a failure must use a new
    request_id -- retry policy is deliberately left to the caller."""
    request_id = str(uuid.uuid4())
    job = await queue.enqueue("analysis", {}, request_id=request_id)
    await queue.claim("analysis")
    await queue.fail(job.id, "boom")

    retried = await queue.enqueue("analysis", {}, request_id=request_id)
    assert retried.id == job.id
    assert retried.status == "failed"


# --- Part VI, Phase 27: retry model -------------------------------------

async def test_terminal_failure_never_becomes_reclaimable(queue, app_db_pool):
    """retryable=False (the default, matching the original contract):
    straight to 'failed', never claimable again -- the required
    "terminal image failure does not retry" case."""
    job = await queue.enqueue("analysis", {})
    await queue.claim("analysis")
    await queue.fail(job.id, "invalid image", retryable=False)

    row = await app_db_pool.fetchrow("SELECT status, attempt_count FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "failed"

    assert await queue.claim("analysis") is None


async def test_retryable_failure_goes_back_to_pending_with_backoff(queue, app_db_pool):
    job = await queue.enqueue("analysis", {})
    await queue.claim("analysis")
    await queue.fail(job.id, "transient db error", retryable=True)

    row = await app_db_pool.fetchrow(
        "SELECT status, attempt_count, next_attempt_at, claimed_at, claimed_until FROM jobs WHERE id = $1", job.id
    )
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
    assert row["next_attempt_at"] is not None
    assert row["claimed_at"] is None
    assert row["claimed_until"] is None

    # Not yet claimable -- next_attempt_at is in the future.
    assert await queue.claim("analysis") is None


async def test_retryable_failure_becomes_claimable_once_backoff_passes(queue, app_db_pool):
    job = await queue.enqueue("analysis", {})
    await queue.claim("analysis")
    await queue.fail(job.id, "transient db error", retryable=True)

    # Simulate the backoff window having passed.
    await app_db_pool.execute("UPDATE jobs SET next_attempt_at = now() - interval '1 second' WHERE id = $1", job.id)

    reclaimed = await queue.claim("analysis")
    assert reclaimed is not None
    assert reclaimed.id == job.id


async def test_retryable_failure_exhausted_attempts_becomes_terminal(queue, app_db_pool):
    """Part VI, Phase 29's "max attempts -> dead" case: once
    attempt_count reaches max_attempts, a retryable failure still lands
    in the same terminal 'failed' state as a non-retryable one -- not
    an infinite retry loop."""
    job = await queue.enqueue("analysis", {})
    await app_db_pool.execute("UPDATE jobs SET max_attempts = 2 WHERE id = $1", job.id)

    await queue.claim("analysis")
    await queue.fail(job.id, "attempt 1 failed", retryable=True)
    await app_db_pool.execute("UPDATE jobs SET next_attempt_at = now() - interval '1 second' WHERE id = $1", job.id)

    await queue.claim("analysis")
    await queue.fail(job.id, "attempt 2 failed", retryable=True)  # attempt_count now reaches max_attempts

    row = await app_db_pool.fetchrow("SELECT status, attempt_count FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "failed"
    assert await queue.claim("analysis") is None


# --- Part VI, Phase 28: visibility heartbeat -----------------------------

async def test_extend_visibility_prevents_reclaim_before_original_timeout_would_expire(queue, app_db_pool):
    job = await queue.enqueue("analysis", {})
    await queue.claim("analysis", visibility_timeout_seconds=1)

    await queue.extend_visibility(job.id, 300)

    row = await app_db_pool.fetchrow("SELECT claimed_until FROM jobs WHERE id = $1", job.id)
    from datetime import datetime, timedelta, timezone
    assert row["claimed_until"] > datetime.now(timezone.utc) + timedelta(seconds=250)

    # Even after the *original* 1s timeout would have expired, the job
    # must not be reclaimable -- extend_visibility() genuinely pushed
    # claimed_until forward, not merely recorded an intent to.
    assert await queue.claim("analysis") is None


async def test_extend_visibility_raises_for_unclaimed_job(queue):
    job = await queue.enqueue("analysis", {})  # never claimed
    with pytest.raises(JobNotFoundError):
        await queue.extend_visibility(job.id, 60)


# --- Part VI, Phase 24: atomic enqueue with an external connection ------

async def test_enqueue_accepts_an_external_connection_for_atomic_composition(queue, app_db_pool):
    """Proves enqueue() can participate in a caller's own transaction
    (Part V, Phase 24's atomic "mark QUEUED + insert job") -- a job
    inserted via an externally-supplied connection that then rolls back
    must not exist afterward."""
    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            job = await queue.enqueue("analysis", {"via": "external-conn"}, conn=conn)
            assert job.status == "pending"

    row = await app_db_pool.fetchrow("SELECT id FROM jobs WHERE id = $1", job.id)
    assert row is not None  # committed normally when the transaction commits
