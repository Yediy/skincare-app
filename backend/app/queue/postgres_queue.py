"""Postgres-table-backed implementation of app.queue.base.JobQueue
(Phase 15). See migration 2e77bc462867 for the schema and the
reasoning behind its two indexes (claim-candidate lookup, idempotent-
enqueue uniqueness) and its deliberate lack of row-level security.
Migration 9db3e5856a79 (Part VI, Phase 27/28) added attempt_count/
max_attempts/next_attempt_at for retry backoff and extend_visibility()
for heartbeating a long-running claim.
"""
import json
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg

from app.queue.base import Job, JobNotFoundError, JobQueue

# Exponential backoff base -- attempt N (0-indexed, i.e. the Nth retry)
# waits BACKOFF_BASE_SECONDS * 2**N before becoming claimable again.
# 30s/60s/120s.../ for a job_type this pass only ever uses for CV
# analysis (seconds-to-tens-of-seconds of real work), not a fabricated
# clinical or SLA number -- a deliberately simple, documented policy.
BACKOFF_BASE_SECONDS = 30


def _row_to_job(row: asyncpg.Record) -> Job:
    payload = row["payload"]
    return Job(
        id=row["id"],
        job_type=row["job_type"],
        payload=json.loads(payload) if isinstance(payload, str) else payload,
        request_id=row["request_id"],
        status=row["status"],
        created_at=row["created_at"],
    )


class PostgresJobQueue(JobQueue):
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def enqueue(
        self,
        job_type: str,
        payload: Dict[str, Any],
        *,
        request_id: Optional[str] = None,
        conn: Optional[asyncpg.Connection] = None,
    ) -> Job:
        if conn is not None:
            return await self._enqueue_with_conn(conn, job_type, payload, request_id)
        async with self._pool.acquire() as acquired:
            return await self._enqueue_with_conn(acquired, job_type, payload, request_id)

    async def _enqueue_with_conn(
        self, conn: asyncpg.Connection, job_type: str, payload: Dict[str, Any], request_id: Optional[str]
    ) -> Job:
        row = await conn.fetchrow(
            """
            INSERT INTO jobs (job_type, payload, request_id)
            VALUES ($1, $2::jsonb, $3)
            ON CONFLICT (job_type, request_id) WHERE request_id IS NOT NULL DO NOTHING
            RETURNING id, job_type, payload, request_id, status, created_at
            """,
            job_type,
            json.dumps(payload),
            request_id,
        )
        if row is not None:
            return _row_to_job(row)

        # ON CONFLICT DO NOTHING returns no row -- a job with this
        # exact (job_type, request_id) already exists. Fetch and
        # return it: the idempotency contract is "one logical job
        # per request_id", not "the first caller wins silently".
        existing = await conn.fetchrow(
            "SELECT id, job_type, payload, request_id, status, created_at "
            "FROM jobs WHERE job_type = $1 AND request_id = $2",
            job_type,
            request_id,
        )
        return _row_to_job(existing)

    async def claim(self, job_type: str, *, visibility_timeout_seconds: int = 300) -> Optional[Job]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE jobs
                SET status = 'claimed',
                    claimed_at = now(),
                    claimed_until = now() + make_interval(secs => $2)
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE job_type = $1
                      AND (
                          (status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
                          OR (status = 'claimed' AND claimed_until < now())
                      )
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, job_type, payload, request_id, status, created_at
                """,
                job_type,
                visibility_timeout_seconds,
            )
        return _row_to_job(row) if row is not None else None

    async def acknowledge(self, job_id: UUID) -> None:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE jobs SET status = 'completed', completed_at = now() "
                "WHERE id = $1 AND status = 'claimed'",
                job_id,
            )
        if result.endswith(" 0"):
            raise JobNotFoundError(str(job_id))

    async def fail(self, job_id: UUID, error: str, *, retryable: bool = False) -> bool:
        async with self._pool.acquire() as conn:
            if retryable:
                row = await conn.fetchrow(
                    "SELECT attempt_count, max_attempts FROM jobs WHERE id = $1 AND status = 'claimed'",
                    job_id,
                )
                if row is not None and row["attempt_count"] + 1 < row["max_attempts"]:
                    backoff_seconds = BACKOFF_BASE_SECONDS * (2 ** row["attempt_count"])
                    result = await conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'pending',
                            last_error = $2,
                            attempt_count = attempt_count + 1,
                            next_attempt_at = now() + make_interval(secs => $3),
                            claimed_at = NULL,
                            claimed_until = NULL
                        WHERE id = $1 AND status = 'claimed'
                        """,
                        job_id, error, backoff_seconds,
                    )
                    if result.endswith(" 0"):
                        raise JobNotFoundError(str(job_id))
                    return False
                # Attempts exhausted (or job already vanished) -- falls
                # through to the same terminal path as retryable=False.

            result = await conn.execute(
                "UPDATE jobs SET status = 'failed', failed_at = now(), last_error = $2, "
                "attempt_count = attempt_count + 1 "
                "WHERE id = $1 AND status = 'claimed'",
                job_id,
                error,
            )
        if result.endswith(" 0"):
            raise JobNotFoundError(str(job_id))
        return True

    async def extend_visibility(self, job_id: UUID, additional_seconds: int) -> None:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE jobs SET claimed_until = now() + make_interval(secs => $2) "
                "WHERE id = $1 AND status = 'claimed'",
                job_id, additional_seconds,
            )
        if result.endswith(" 0"):
            raise JobNotFoundError(str(job_id))
