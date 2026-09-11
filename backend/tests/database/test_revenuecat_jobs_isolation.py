"""Migration 4e5cda3a6bb0's core claim: the shared `jobs` queue table
is genuinely, database-level isolated by job_type between the ordinary
`skincare_app` role (which owns every non-billing job_type -- today
just `analysis`) and the dedicated `skincare_billing` role (which owns
only `revenuecat_webhook`). Application-layer convention (the billing
code simply never happening to pass another job_type) is explicitly
NOT what this test file proves -- see
tests/database/test_revenuecat_billing_privilege.py's own docstring
for the same "prove the boundary itself, not the caller's good
behavior" standard. Every test below runs through the real restricted
roles (app_db_pool/billing_db_pool), never the superuser.
"""
import asyncpg
import pytest

from app.queue.base import JobNotFoundError
from app.queue.postgres_queue import PostgresJobQueue

ANALYSIS_JOB_TYPE = "analysis"
REVENUECAT_WEBHOOK_JOB_TYPE = "revenuecat_webhook"


# ---------------------------------------------------------------------------
# skincare_billing: can fully operate its own job_type...
# ---------------------------------------------------------------------------


async def test_billing_role_can_enqueue_a_revenuecat_webhook_job(billing_db_pool):
    queue = PostgresJobQueue(billing_db_pool)
    job = await queue.enqueue(REVENUECAT_WEBHOOK_JOB_TYPE, {"webhook_event_id": "e1"})
    assert job.job_type == REVENUECAT_WEBHOOK_JOB_TYPE
    assert job.status == "pending"


async def test_billing_role_can_claim_a_revenuecat_webhook_job(billing_db_pool):
    queue = PostgresJobQueue(billing_db_pool)
    enqueued = await queue.enqueue(REVENUECAT_WEBHOOK_JOB_TYPE, {"webhook_event_id": "e2"})
    claimed = await queue.claim(REVENUECAT_WEBHOOK_JOB_TYPE)
    assert claimed.id == enqueued.id
    assert claimed.status == "claimed"


async def test_billing_role_can_acknowledge_a_revenuecat_webhook_job(billing_db_pool):
    queue = PostgresJobQueue(billing_db_pool)
    await queue.enqueue(REVENUECAT_WEBHOOK_JOB_TYPE, {"webhook_event_id": "e3"})
    claimed = await queue.claim(REVENUECAT_WEBHOOK_JOB_TYPE)
    await queue.acknowledge(claimed.id)

    row = await billing_db_pool.fetchrow("SELECT status FROM jobs WHERE id = $1", claimed.id)
    assert row["status"] == "completed"


async def test_billing_role_can_fail_a_revenuecat_webhook_job(billing_db_pool):
    queue = PostgresJobQueue(billing_db_pool)
    await queue.enqueue(REVENUECAT_WEBHOOK_JOB_TYPE, {"webhook_event_id": "e4"})
    claimed = await queue.claim(REVENUECAT_WEBHOOK_JOB_TYPE)
    is_terminal = await queue.fail(claimed.id, "PROCESSING_ERROR", retryable=False)
    assert is_terminal is True

    row = await billing_db_pool.fetchrow("SELECT status FROM jobs WHERE id = $1", claimed.id)
    assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# ...and nothing else. `analysis` jobs are invisible and unwritable
# through the billing role, even when a real one already exists.
# ---------------------------------------------------------------------------


async def test_billing_role_cannot_select_an_analysis_job(app_db_pool, billing_db_pool):
    app_queue = PostgresJobQueue(app_db_pool)
    analysis_job = await app_queue.enqueue(ANALYSIS_JOB_TYPE, {"analysis_request_id": "r1"})

    row = await billing_db_pool.fetchrow("SELECT * FROM jobs WHERE id = $1", analysis_job.id)
    assert row is None


async def test_billing_role_cannot_claim_an_analysis_job(app_db_pool, billing_db_pool):
    app_queue = PostgresJobQueue(app_db_pool)
    await app_queue.enqueue(ANALYSIS_JOB_TYPE, {"analysis_request_id": "r2"})

    billing_queue = PostgresJobQueue(billing_db_pool)
    claimed = await billing_queue.claim(ANALYSIS_JOB_TYPE)
    assert claimed is None


async def test_billing_role_cannot_update_an_analysis_job(app_db_pool, billing_db_pool, db_pool):
    app_queue = PostgresJobQueue(app_db_pool)
    analysis_job = await app_queue.enqueue(ANALYSIS_JOB_TYPE, {"analysis_request_id": "r3"})

    result = await billing_db_pool.execute(
        "UPDATE jobs SET status = 'claimed', claimed_at = now(), "
        "claimed_until = now() + interval '5 minutes' WHERE id = $1",
        analysis_job.id,
    )
    assert result == "UPDATE 0"

    # The superuser view proves the row itself was never touched -- not
    # merely that the billing role's own view of it looked unchanged.
    row = await db_pool.fetchrow("SELECT status FROM jobs WHERE id = $1", analysis_job.id)
    assert row["status"] == "pending"


async def test_billing_role_cannot_mark_an_analysis_job_complete(app_db_pool, billing_db_pool):
    app_queue = PostgresJobQueue(app_db_pool)
    analysis_job = await app_queue.enqueue(ANALYSIS_JOB_TYPE, {"analysis_request_id": "r4"})
    claimed = await app_queue.claim(ANALYSIS_JOB_TYPE)
    assert claimed.id == analysis_job.id

    billing_queue = PostgresJobQueue(billing_db_pool)
    with pytest.raises(JobNotFoundError):
        await billing_queue.acknowledge(analysis_job.id)


async def test_billing_role_cannot_mark_an_analysis_job_failed(app_db_pool, billing_db_pool):
    app_queue = PostgresJobQueue(app_db_pool)
    analysis_job = await app_queue.enqueue(ANALYSIS_JOB_TYPE, {"analysis_request_id": "r5"})
    await app_queue.claim(ANALYSIS_JOB_TYPE)

    billing_queue = PostgresJobQueue(billing_db_pool)
    with pytest.raises(JobNotFoundError):
        await billing_queue.fail(analysis_job.id, "SHOULD_NOT_BE_REACHABLE", retryable=False)


async def test_billing_role_cannot_insert_a_job_of_a_non_billing_type(billing_db_pool):
    queue = PostgresJobQueue(billing_db_pool)
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await queue.enqueue(ANALYSIS_JOB_TYPE, {"analysis_request_id": "r6"})


# ---------------------------------------------------------------------------
# The reverse boundary: the ordinary application role cannot see or
# operate on `revenuecat_webhook` jobs either -- the isolation is
# symmetric, not merely "billing can't touch analysis".
# ---------------------------------------------------------------------------


async def test_app_role_cannot_select_a_revenuecat_webhook_job(app_db_pool, billing_db_pool):
    billing_queue = PostgresJobQueue(billing_db_pool)
    webhook_job = await billing_queue.enqueue(REVENUECAT_WEBHOOK_JOB_TYPE, {"webhook_event_id": "e5"})

    row = await app_db_pool.fetchrow("SELECT * FROM jobs WHERE id = $1", webhook_job.id)
    assert row is None


async def test_app_role_cannot_claim_a_revenuecat_webhook_job(app_db_pool, billing_db_pool):
    billing_queue = PostgresJobQueue(billing_db_pool)
    await billing_queue.enqueue(REVENUECAT_WEBHOOK_JOB_TYPE, {"webhook_event_id": "e6"})

    app_queue = PostgresJobQueue(app_db_pool)
    claimed = await app_queue.claim(REVENUECAT_WEBHOOK_JOB_TYPE)
    assert claimed is None


# ---------------------------------------------------------------------------
# The existing analysis queue path, through the ordinary role, is
# unaffected by any of the above -- not merely asserted here but
# exercised in full by tests/queue/test_postgres_job_queue.py and
# tests/workers/test_analysis_worker.py, run unmodified in the same
# suite.
# ---------------------------------------------------------------------------


async def test_app_role_analysis_queue_lifecycle_unaffected(app_db_pool):
    queue = PostgresJobQueue(app_db_pool)
    job = await queue.enqueue(ANALYSIS_JOB_TYPE, {"analysis_request_id": "r7"})
    claimed = await queue.claim(ANALYSIS_JOB_TYPE)
    assert claimed.id == job.id
    await queue.acknowledge(claimed.id)

    row = await app_db_pool.fetchrow("SELECT status FROM jobs WHERE id = $1", job.id)
    assert row["status"] == "completed"
