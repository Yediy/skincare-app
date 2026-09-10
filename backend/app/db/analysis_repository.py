"""Persistence for the full analysis lifecycle (Part III, migration
b034483cb876) -- analysis_requests/analysis_results/
analysis_measurements/analysis_product_recommendations, all user-owned,
all RLS-protected (same app.current_user_id pattern as every other
RLS table in this repository).

commit_analysis_result() is Part VI, Phase 30's required atomic commit:
one transaction inserts the result, every measurement, every product
recommendation decision (each idempotent via the schema's own UNIQUE
constraints + ON CONFLICT DO NOTHING -- never a duplicate write on a
worker retry), marks the request COMPLETED, and consumes the usage
reservation -- all four state changes commit together or not at all.
If the worker dies after this transaction commits but before
deleting the ephemeral image, the result is already durably correct;
cleanup removes the object later (Part IV). If the worker retries
after a crash *before* this transaction committed, it re-runs the
whole thing from scratch and this function's own idempotency makes
that safe.
"""
import json
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

RECEIVED = "RECEIVED"
QUEUED = "QUEUED"
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"


async def create_request(
    pool: asyncpg.Pool,
    user_id: UUID,
    request_id: str,
    *,
    usage_reservation_id: Optional[UUID] = None,
    home_region: Optional[str] = None,
    cell_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Idempotent by request_id (schema UNIQUE) -- ON CONFLICT DO
    NOTHING then fetch, the same idempotent-create pattern as
    PostgresJobQueue.enqueue()/usage_repository.reserve(). A retried
    POST /api/v2/analyses with the same request_id returns the SAME
    row, never a duplicate."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            row = await conn.fetchrow(
                """
                INSERT INTO analysis_requests
                    (user_id, request_id, status, usage_reservation_id, home_region, cell_id)
                VALUES ($1, $2, 'RECEIVED', $3, $4, $5)
                ON CONFLICT (request_id) DO NOTHING
                RETURNING id, user_id, request_id, status, attempt_count, created_at
                """,
                user_id, request_id, usage_reservation_id, home_region, cell_id,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT id, user_id, request_id, status, attempt_count, created_at "
                    "FROM analysis_requests WHERE request_id = $1",
                    request_id,
                )
    return dict(row)


async def get_request_by_id(pool: asyncpg.Pool, user_id: UUID, analysis_request_id: UUID) -> Optional[Dict[str, Any]]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            row = await conn.fetchrow(
                "SELECT * FROM analysis_requests WHERE id = $1", analysis_request_id
            )
    return dict(row) if row is not None else None


async def get_request_by_request_id(pool: asyncpg.Pool, user_id: UUID, request_id: str) -> Optional[Dict[str, Any]]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            row = await conn.fetchrow(
                "SELECT * FROM analysis_requests WHERE request_id = $1", request_id
            )
    return dict(row) if row is not None else None


async def mark_queued(
    pool: asyncpg.Pool,
    user_id: UUID,
    analysis_request_id: UUID,
    *,
    image_object_key: str,
    image_expires_at,
    conn: Optional[asyncpg.Connection] = None,
) -> None:
    """`conn`, when given, is used directly instead of acquiring+
    transacting a new connection -- lets AnalysisSubmissionService
    (Part V, Phase 24) run this UPDATE in the same transaction as
    PostgresJobQueue.enqueue()'s job INSERT, so a request is never left
    QUEUED with no corresponding job (or vice versa). The caller is
    responsible for having already opened that transaction and set
    app.current_user_id is set fresh here regardless, since it's
    transaction-local (`true`) and this statement needs it in scope
    either way."""
    if conn is not None:
        await _mark_queued_with_conn(conn, user_id, analysis_request_id, image_object_key, image_expires_at)
        return
    async with pool.acquire() as acquired:
        async with acquired.transaction():
            await _mark_queued_with_conn(acquired, user_id, analysis_request_id, image_object_key, image_expires_at)


async def _mark_queued_with_conn(conn, user_id: UUID, analysis_request_id: UUID, image_object_key: str, image_expires_at) -> None:
    await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
    await conn.execute(
        """
        UPDATE analysis_requests
        SET status = 'QUEUED', queued_at = now(), image_object_key = $2, image_expires_at = $3
        WHERE id = $1
        """,
        analysis_request_id, image_object_key, image_expires_at,
    )


async def mark_processing(pool: asyncpg.Pool, user_id: UUID, analysis_request_id: UUID) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            await conn.execute(
                "UPDATE analysis_requests SET status = 'PROCESSING', started_at = now() WHERE id = $1",
                analysis_request_id,
            )


async def mark_failed(pool: asyncpg.Pool, user_id: UUID, analysis_request_id: UUID, error_code: str) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            await conn.execute(
                "UPDATE analysis_requests SET status = 'FAILED', failed_at = now(), error_code = $2 WHERE id = $1",
                analysis_request_id, error_code,
            )


async def increment_attempt_count(pool: asyncpg.Pool, user_id: UUID, analysis_request_id: UUID) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            await conn.execute(
                "UPDATE analysis_requests SET attempt_count = attempt_count + 1 WHERE id = $1",
                analysis_request_id,
            )


async def get_result(pool: asyncpg.Pool, user_id: UUID, analysis_request_id: UUID) -> Optional[Dict[str, Any]]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            row = await conn.fetchrow(
                "SELECT * FROM analysis_results WHERE analysis_request_id = $1", analysis_request_id
            )
    if row is None:
        return None
    result = dict(row)
    for key in ("capture_assessment", "scores", "plan"):
        if isinstance(result[key], str):
            result[key] = json.loads(result[key])
    return result


async def get_product_recommendations(pool: asyncpg.Pool, user_id: UUID, analysis_request_id: UUID) -> List[Dict[str, Any]]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            rows = await conn.fetch(
                "SELECT * FROM analysis_product_recommendations WHERE analysis_request_id = $1 ORDER BY plan_step_key",
                analysis_request_id,
            )
    results = []
    for r in rows:
        rec = dict(r)
        for key in ("reason_codes", "restrictions"):
            if isinstance(rec[key], str):
                rec[key] = json.loads(rec[key])
        results.append(rec)
    return results


async def find_overdue_ephemeral_images(pool: asyncpg.Pool, *, batch_limit: int = 100) -> List[Dict[str, Any]]:
    """Cross-user by design -- the safety-net cleanup sweeper (Part IV,
    Phase 22) needs to find overdue images regardless of which user
    they belong to. Goes through find_overdue_ephemeral_images(), a
    narrow SECURITY DEFINER SQL function (migration 071fab81f0ac), not
    a direct SELECT -- RLS would otherwise scope any ordinary query to
    one app.current_user_id, and this role has no broader bypass."""
    rows = await pool.fetch("SELECT * FROM find_overdue_ephemeral_images($1)", batch_limit)
    return [dict(r) for r in rows]


async def clear_image_reference(pool: asyncpg.Pool, analysis_request_id: UUID) -> None:
    """Call only after the object has been *confirmed* deleted from
    storage (EphemeralAnalysisImageStore.delete_if_confirmed()
    returning True) -- never before, or a crash between clearing this
    reference and actually deleting the object would orphan it with no
    remaining pointer for a later sweep to find."""
    await pool.execute("SELECT clear_image_reference($1)", analysis_request_id)


async def commit_analysis_result(
    pool: asyncpg.Pool,
    user_id: UUID,
    analysis_request_id: UUID,
    *,
    capture_assessment: Dict[str, Any],
    scores: Dict[str, Any],
    plan: Dict[str, Any],
    eligible_for_longitudinal_comparison: bool,
    pipeline_version: str,
    metric_results: Dict[str, Dict[str, Any]],
    product_recommendations: List[Dict[str, Any]],
    usage_reservation_id: UUID,
) -> None:
    """Part VI, Phase 30's required atomic commit. See module docstring."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))

            await conn.execute(
                """
                INSERT INTO analysis_results
                    (analysis_request_id, user_id, capture_assessment, scores, plan,
                     eligible_for_longitudinal_comparison, pipeline_version)
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6, $7)
                ON CONFLICT (analysis_request_id) DO NOTHING
                """,
                analysis_request_id, user_id, json.dumps(capture_assessment), json.dumps(scores),
                json.dumps(plan), eligible_for_longitudinal_comparison, pipeline_version,
            )

            for metric_name, m in metric_results.items():
                await conn.execute(
                    """
                    INSERT INTO analysis_measurements
                        (analysis_request_id, user_id, metric_name, value, confidence, status,
                         uncertainty_reasons, metric_version, calibration_version)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                    ON CONFLICT (analysis_request_id, metric_name) DO NOTHING
                    """,
                    analysis_request_id, user_id, metric_name, m.get("value"), m.get("confidence") or 0,
                    m.get("status", "VALID"), json.dumps(m.get("uncertainty_reasons") or []),
                    m.get("metric_version"), m.get("calibration_version"),
                )

            for rec in product_recommendations:
                await conn.execute(
                    """
                    INSERT INTO analysis_product_recommendations
                        (analysis_request_id, user_id, plan_step_key, product_id, formulation_id,
                         rank_position, safety_status, reason_codes, restrictions, rules_version)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10)
                    ON CONFLICT (analysis_request_id, plan_step_key) DO NOTHING
                    """,
                    analysis_request_id, user_id, rec["plan_step_key"], rec["product_id"], rec["formulation_id"],
                    rec["rank_position"], rec["safety_status"], json.dumps(rec["reason_codes"]),
                    json.dumps(rec["restrictions"]), rec["rules_version"],
                )

            await conn.execute(
                "UPDATE analysis_requests SET status = 'COMPLETED', completed_at = now() "
                "WHERE id = $1 AND status != 'COMPLETED'",
                analysis_request_id,
            )

            # Same transaction, same database -- consuming the usage
            # reservation here (rather than a separate call after
            # commit) is what actually makes "quota consumed exactly
            # once" atomic with "result persisted", not just
            # sequential and hopeful.
            await conn.execute(
                "UPDATE analysis_usage SET status = 'CONSUMED', consumed_at = now() "
                "WHERE id = $1 AND status = 'RESERVED'",
                usage_reservation_id,
            )
