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
from datetime import datetime
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


async def record_ephemeral_image_reference(
    pool: asyncpg.Pool,
    user_id: UUID,
    analysis_request_id: UUID,
    *,
    image_object_key: str,
    image_expires_at,
) -> None:
    """Persists the image reference WITHOUT touching status -- used
    only by AnalysisSubmissionService's failure-window-B compensation
    (Part V, this pass), when the atomic mark_queued()+enqueue()
    transaction that would normally set these same columns failed to
    commit. A direct, independent write so the cleanup sweeper
    (find_overdue_ephemeral_images(), driven purely by
    image_expires_at) can still recover the orphaned object later,
    even though the request itself never reached QUEUED."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            await conn.execute(
                "UPDATE analysis_requests SET image_object_key = $2, image_expires_at = $3 WHERE id = $1",
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
    """Mobile V1 Phase C1: client-facing product-recommendation
    projection, never `SELECT *` -- same reasoning as
    get_measurements()'s own docstring. `id`/`user_id`/
    `analysis_request_id`/`created_at` are internal bookkeeping with no
    place in this response, RLS already scopes the row to its owner
    regardless.

    Display metadata (`brand`/`product_name`/`verification_date`)
    prefers this row's own historical snapshot
    (`brand_name_snapshot`/`product_name_snapshot`/
    `verification_date_snapshot`, populated for every analysis
    completed after migration 366861ec262d) and falls back to a
    read-only join against the *current* catalog only when the
    snapshot is NULL (older, pre-snapshot analyses) -- this is a
    display convenience for historical rows, never a mutation of the
    historical row itself, and it never fabricates a verification date
    or a display name: a formulation/product/brand that can't be
    resolved either way simply returns null, per this phase's own
    "do not invent a label" requirement."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            rows = await conn.fetch(
                """
                SELECT
                    apr.plan_step_key,
                    apr.product_id,
                    apr.formulation_id,
                    apr.rank_position,
                    apr.safety_status,
                    apr.reason_codes,
                    apr.restrictions,
                    apr.rules_version,
                    COALESCE(apr.brand_name_snapshot, b.name) AS brand,
                    COALESCE(apr.product_name_snapshot, p.name) AS product_name,
                    COALESCE(apr.verification_date_snapshot, pf.verified_at) AS verification_date
                FROM analysis_product_recommendations apr
                LEFT JOIN products p ON p.id = apr.product_id
                LEFT JOIN brands b ON b.id = p.brand_id
                LEFT JOIN product_formulations pf ON pf.id = apr.formulation_id
                WHERE apr.analysis_request_id = $1
                ORDER BY apr.plan_step_key
                """,
                analysis_request_id,
            )
    results = []
    for r in rows:
        rec = dict(r)
        for key in ("reason_codes", "restrictions"):
            if isinstance(rec[key], str):
                rec[key] = json.loads(rec[key])
        if rec["verification_date"] is not None:
            rec["verification_date"] = rec["verification_date"].isoformat()
        for key in ("product_id", "formulation_id"):
            rec[key] = str(rec[key])
        results.append(rec)
    return results


async def get_measurements(pool: asyncpg.Pool, user_id: UUID, analysis_request_id: UUID) -> List[Dict[str, Any]]:
    """Mobile V1 Phase B: per-metric detail (value/confidence/status/
    uncertainty_reasons) was persisted by commit_analysis_result() from
    day one, but nothing previously read it back out -- GET
    /api/v2/analyses/{id} exposed only the aggregate `scores` blob,
    never the per-metric VALID/BORDERLINE/ABSTAINED breakdown a client
    needs to render abstention/uncertainty honestly (never a fabricated
    numeric value for an ABSTAINED metric). Additive-only: a new field
    on the existing response, nothing removed or reshaped.

    Post-merge audit repair (section 9): projects only the columns the
    mobile/API contract actually documents, never `SELECT *`. RLS
    already scopes this row to its owner regardless -- this is a
    separate, deliberate concern: `id`/`analysis_request_id`/`user_id`/
    `created_at` are internal database identifiers/bookkeeping with no
    place in a client-facing response, not secrets being protected from
    another user. Adding a new internal column to this table must never
    silently start appearing in this API response."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            rows = await conn.fetch(
                """
                SELECT metric_name, value, confidence, status, uncertainty_reasons,
                       metric_version, calibration_version
                FROM analysis_measurements
                WHERE analysis_request_id = $1
                ORDER BY metric_name
                """,
                analysis_request_id,
            )
    results = []
    for r in rows:
        m = dict(r)
        if isinstance(m["uncertainty_reasons"], str):
            m["uncertainty_reasons"] = json.loads(m["uncertainty_reasons"])
        if m["value"] is not None:
            m["value"] = float(m["value"])
        m["confidence"] = float(m["confidence"])
        results.append(m)
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
                # Historical display snapshot (Mobile V1 Phase C1) --
                # exactly the brand/product_name/verification_date
                # StepProductRecommendation already carried at
                # recommendation time, so a completed analysis never
                # has to be re-resolved against mutable current catalog
                # state to render what it actually recommended.
                # rec["verification_date"] is an ISO-8601 string
                # (StepProductRecommendation.to_dict()) or None --
                # asyncpg binds a timestamptz parameter as a real
                # datetime, never a string it parses itself server-side,
                # so it's converted here rather than relying on a SQL
                # ::timestamptz cast to do it.
                verification_date_snapshot = rec.get("verification_date")
                if isinstance(verification_date_snapshot, str):
                    verification_date_snapshot = datetime.fromisoformat(verification_date_snapshot)

                await conn.execute(
                    """
                    INSERT INTO analysis_product_recommendations
                        (analysis_request_id, user_id, plan_step_key, product_id, formulation_id,
                         rank_position, safety_status, reason_codes, restrictions, rules_version,
                         brand_name_snapshot, product_name_snapshot, verification_date_snapshot)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10, $11, $12, $13)
                    ON CONFLICT (analysis_request_id, plan_step_key) DO NOTHING
                    """,
                    analysis_request_id, user_id, rec["plan_step_key"], rec["product_id"], rec["formulation_id"],
                    rec["rank_position"], rec["safety_status"], json.dumps(rec["reason_codes"]),
                    json.dumps(rec["restrictions"]), rec["rules_version"],
                    rec.get("brand"), rec.get("product_name"), verification_date_snapshot,
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
