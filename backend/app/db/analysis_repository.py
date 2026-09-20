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


class AnalysisResultCommitFencedError(Exception):
    """System Integrity Gate V1: raised by commit_analysis_result() when
    `processing_claim_token` no longer matches analysis_requests.
    processing_claim_token -- a newer worker already installed its own
    token via mark_processing() before this caller's commit ran.
    Nothing from this call is persisted (the whole transaction rolls
    back). Callers (app/domain/analysis_execution_service.py's
    execute()) translate this into AnalysisExecutionLeaseLostError --
    this module deliberately raises its own, repository-level
    exception rather than importing that one, to avoid a circular
    import between the two."""


RECEIVED = "RECEIVED"
QUEUED = "QUEUED"
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

# Bumped only if the shape of the display-snapshot contract itself
# changes (see migration e421ed4cf053). commit_analysis_result() is
# the sole production writer of analysis_product_recommendations, and
# it always captures brand/product_name/verification_date at the
# moment a recommendation is built -- so every row it inserts is
# stamped with this version, independent of whether any individual
# snapshot field happens to be NULL. A row with no version at all is
# how a genuinely legacy (pre-e421ed4cf053) row is identified.
DISPLAY_SNAPSHOT_VERSION = 1


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
                ON CONFLICT (user_id, request_id) DO NOTHING
                RETURNING id, user_id, request_id, status, attempt_count, created_at
                """,
                user_id, request_id, usage_reservation_id, home_region, cell_id,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT id, user_id, request_id, status, attempt_count, created_at "
                    "FROM analysis_requests WHERE user_id = $1 AND request_id = $2",
                    user_id, request_id,
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


async def list_requests_by_user(
    pool: asyncpg.Pool,
    user_id: UUID,
    *,
    limit: int,
    before_created_at: Optional[datetime] = None,
    before_id: Optional[UUID] = None,
) -> List[Dict[str, Any]]:
    """Mobile C3 (history): keyset pagination on (created_at, id) DESC
    -- never a naive OFFSET, which would skip/duplicate rows if a new
    analysis is created between two page fetches. No explicit
    `WHERE user_id = ...`, same as every other function in this module
    -- RLS (analysis_requests_isolation) is what actually scopes every
    row to app.current_user_id, set below; this is proven directly by
    test (another user's rows never appear regardless of what caller
    code does or doesn't filter). `(created_at, id) < (cursor)` is a
    strict tuple comparison, so it excludes the cursor row itself and
    is stable even when two rows share the same created_at timestamp
    (id is always unique). Caller passes `limit + 1` to detect
    `has_more` without a separate COUNT query."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            if before_created_at is not None and before_id is not None:
                rows = await conn.fetch(
                    """
                    SELECT id, request_id, status, error_code, created_at, completed_at
                    FROM analysis_requests
                    WHERE (created_at, id) < ($2, $3)
                    ORDER BY created_at DESC, id DESC
                    LIMIT $1
                    """,
                    limit, before_created_at, before_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, request_id, status, error_code, created_at, completed_at
                    FROM analysis_requests
                    ORDER BY created_at DESC, id DESC
                    LIMIT $1
                    """,
                    limit,
                )
    return [dict(row) for row in rows]


async def get_request_by_request_id(pool: asyncpg.Pool, user_id: UUID, request_id: str) -> Optional[Dict[str, Any]]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            row = await conn.fetchrow(
                "SELECT * FROM analysis_requests WHERE user_id = $1 AND request_id = $2", user_id, request_id
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


async def mark_processing(
    pool: asyncpg.Pool, user_id: UUID, analysis_request_id: UUID, processing_claim_token: Optional[UUID] = None,
    *, job_id: Optional[UUID] = None,
) -> bool:
    """Installs `processing_claim_token` as this request's current
    processing owner (System Integrity Gate V1, section 2).

    Independent-review follow-up (the ENTRY-side race the original
    section-2 fix missed): calling mark_processing() is the act of
    *becoming* the current owner, so on its own it has nothing to
    fence against -- but "on its own" was exactly the bug. Without
    proof that the caller's `processing_claim_token` is still the
    *queue's* current claim token for the *actual* job driving this
    attempt, a stale worker A (claimed, then suspended past its lease
    expiry while a legitimate worker B reclaimed the same job) could
    resume and simply overwrite B's already-installed
    processing_claim_token with its own stale one -- reversing
    legitimate ownership, the mirror image of the exit-side race
    section 2 already closed.

    When `job_id` is given, the install is now proven atomically, in
    the SAME statement, against the live `jobs` row: it must exist,
    have `job_type = 'analysis'`, still be `status = 'claimed'`, still
    carry exactly this `processing_claim_token` as its `claim_token`,
    and -- the durable, normalized relationship
    AnalysisSubmissionService itself establishes at submission time,
    not a job_id/analysis_request_id pair parsed back out of arbitrary
    JSON payload -- have a `request_id` equal to this analysis
    request's own `request_id`. `job_id` alone would leave a caller
    trusting `claim_token` as a bare, globally-unique secret; requiring
    both plus the request_id relationship means a token from some
    *other* job/analysis-request pair can never install ownership
    here even in the astronomically unlikely event two live claims
    ever shared a token. Deliberately one UPDATE, not a SELECT-to-
    prove-ownership followed by a separate UPDATE -- splitting those
    into two statements (or two transactions) would reopen exactly the
    TOCTOU window this closes.

    `job_id=None` (the default) skips that proof entirely -- correct
    only for a caller with no real queue job to prove ownership
    against (a direct call/test, mirroring `processing_claim_token`'s
    own None-means-no-token-tracking default) -- and falls back to the
    original QUEUED/PROCESSING-gated install (also enforced
    independently at the database level by migration 44a74f2a79a7's
    transition trigger for the QUEUED->PROCESSING case;
    PROCESSING->PROCESSING doesn't change `status` at all, so the
    trigger never even fires for a legitimate re-entrant claim).

    Returns True if this call actually installed the token, False if
    it was fenced out -- status was no longer QUEUED/PROCESSING, or
    (when `job_id` was given) the ownership proof failed: the job is
    no longer claimed, is claimed by a different token, or doesn't
    correspond to this analysis request. Callers
    (AnalysisExecutionService.execute()) must treat False as STALE
    ATTEMPT / ABANDON -- never retry the same install, never proceed
    to retrieve the image or run compute."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            if job_id is not None:
                row = await conn.fetchrow(
                    """
                    UPDATE analysis_requests
                    SET status = 'PROCESSING', started_at = now(), processing_claim_token = $2
                    WHERE id = $1
                      AND status IN ('QUEUED', 'PROCESSING')
                      AND EXISTS (
                          SELECT 1 FROM jobs j
                          WHERE j.id = $3
                            AND j.job_type = 'analysis'
                            AND j.status = 'claimed'
                            AND j.claim_token = $2
                            AND j.request_id = analysis_requests.request_id
                      )
                    RETURNING id
                    """,
                    analysis_request_id, processing_claim_token, job_id,
                )
            else:
                row = await conn.fetchrow(
                    """
                    UPDATE analysis_requests
                    SET status = 'PROCESSING', started_at = now(), processing_claim_token = $2
                    WHERE id = $1 AND status IN ('QUEUED', 'PROCESSING')
                    RETURNING id
                    """,
                    analysis_request_id, processing_claim_token,
                )
    return row is not None


async def mark_failed(
    pool: asyncpg.Pool, user_id: UUID, analysis_request_id: UUID, error_code: str,
    *, processing_claim_token: Optional[UUID] = None,
) -> bool:
    """Marks a non-terminal request FAILED. Returns True if this call
    actually performed that transition, False if it was fenced out
    (System Integrity Gate V1, section 2) -- the request either already
    reached a terminal state, or its current `processing_claim_token`
    belongs to a different (newer) worker than the one this caller
    presented.

    The fencing predicate is deliberately asymmetric: a NULL
    `processing_claim_token` column (never entered processing at all --
    e.g. failing at RECEIVED/QUEUED before any worker claimed it) is
    fair game for ANY caller, including one that itself passes None
    (a caller with no queue-level claim to present, e.g. most of this
    codebase's own direct tests) -- but a caller that passes None
    against a row some real worker DOES currently own (a non-NULL
    column value) is correctly rejected: presenting no token is not
    proof of ownership over a row something else legitimately claimed.
    Callers (app/domain/analysis_execution_service.py's
    mark_terminal_failure()) must check this return value BEFORE
    performing any compensation (releasing quota, deleting the image)
    -- never after, and never unconditionally."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            row = await conn.fetchrow(
                """
                UPDATE analysis_requests
                SET status = 'FAILED', failed_at = now(), error_code = $2, processing_claim_token = NULL
                WHERE id = $1
                  AND status IN ('RECEIVED', 'QUEUED', 'PROCESSING')
                  AND (processing_claim_token IS NULL OR processing_claim_token = $3)
                RETURNING id
                """,
                analysis_request_id, error_code, processing_claim_token,
            )
    return row is not None


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

    Display metadata (`brand`/`product_name`/`verification_date`) is
    governed by `display_snapshot_version` (migration e421ed4cf053),
    not by per-field NULL-ness -- a NULL snapshot field on a
    versioned row is itself authoritative (e.g. a formulation that
    genuinely had no verification date at analysis time) and must
    never be papered over with a later current-catalog value:

      - `display_snapshot_version IS NOT NULL` -- this row went
        through the display-snapshot contract at commit time.
        `brand_name_snapshot`/`product_name_snapshot`/
        `verification_date_snapshot` are authoritative as written,
        NULL or not. No current-catalog fallback, field by field or
        otherwise -- that would let a later catalog edit (a rename, a
        formulation getting verified after the fact) leak into an
        already-completed analysis's historical display.

      - `display_snapshot_version IS NULL` -- genuinely legacy (older
        than migration e421ed4cf053, or e421ed4cf053, or
        366861ec262d before it). Falls back to a read-only join
        against the *current* catalog -- a display convenience for
        historical rows that predate snapshotting, never a mutation
        of the historical row itself, and never a fabricated
        verification date: a formulation/product/brand that can't be
        resolved either way simply returns null.

    The formulation join binds both `pf.id = apr.formulation_id AND
    pf.product_id = apr.product_id` (rather than trusting the two
    independent FKs alone to imply the ids belong together) so the
    legacy-fallback projection stays internally consistent even under
    corrupted/manually-edited historical data."""
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
                    CASE WHEN apr.display_snapshot_version IS NOT NULL
                         THEN apr.brand_name_snapshot ELSE b.name END AS brand,
                    CASE WHEN apr.display_snapshot_version IS NOT NULL
                         THEN apr.product_name_snapshot ELSE p.name END AS product_name,
                    CASE WHEN apr.display_snapshot_version IS NOT NULL
                         THEN apr.verification_date_snapshot ELSE pf.verified_at END AS verification_date
                FROM analysis_product_recommendations apr
                LEFT JOIN products p ON p.id = apr.product_id
                LEFT JOIN brands b ON b.id = p.brand_id
                LEFT JOIN product_formulations pf
                    ON pf.id = apr.formulation_id AND pf.product_id = apr.product_id
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
    processing_claim_token: Optional[UUID] = None,
) -> None:
    """Part VI, Phase 30's required atomic commit. See module docstring.

    System Integrity Gate V1, section 2: the whole transaction is
    fenced on `processing_claim_token` still being this request's
    current analysis_requests.processing_claim_token -- checked FIRST,
    before any of the result/measurement/recommendation inserts below,
    so a stale caller's attempt is rejected (AnalysisResultCommitFencedError,
    the whole transaction rolled back, nothing persisted) rather than
    landing partial writes a legitimate newer worker's own commit would
    then have to coexist with. A newer worker's subsequent commit (using
    its own, current token) is completely unaffected either way: every
    insert below is idempotent (ON CONFLICT DO NOTHING keyed by
    analysis_request_id), so whichever of two racing commits actually
    lands first is irrelevant to which one succeeds -- only the token
    check is."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))

            # Same asymmetric predicate as mark_failed(): a NULL column
            # (no token was ever installed for this request -- e.g. a
            # direct call/test that never threads a claim_token through
            # mark_processing()) is fair game for any caller, including
            # one that itself passes None; a non-NULL column value must
            # match the caller's token exactly.
            fenced = await conn.fetchrow(
                """
                UPDATE analysis_requests
                SET status = 'COMPLETED', completed_at = now(), processing_claim_token = NULL
                WHERE id = $1 AND status = 'PROCESSING'
                  AND (processing_claim_token IS NULL OR processing_claim_token = $2)
                RETURNING id
                """,
                analysis_request_id, processing_claim_token,
            )
            if fenced is None:
                raise AnalysisResultCommitFencedError(str(analysis_request_id))

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
                # DISPLAY_SNAPSHOT_VERSION is stamped on every row this
                # function writes, marking it (vs. a genuinely legacy,
                # pre-e421ed4cf053 row) as one where get_product_
                # recommendations() must treat these snapshot columns
                # as authoritative even when NULL -- see that
                # function's own docstring.
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
                         brand_name_snapshot, product_name_snapshot, verification_date_snapshot,
                         display_snapshot_version)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10, $11, $12, $13, $14)
                    ON CONFLICT (analysis_request_id, plan_step_key) DO NOTHING
                    """,
                    analysis_request_id, user_id, rec["plan_step_key"], rec["product_id"], rec["formulation_id"],
                    rec["rank_position"], rec["safety_status"], json.dumps(rec["reason_codes"]),
                    json.dumps(rec["restrictions"]), rec["rules_version"],
                    rec.get("brand"), rec.get("product_name"), verification_date_snapshot,
                    DISPLAY_SNAPSHOT_VERSION,
                )

            # analysis_requests itself was already fenced and moved to
            # COMPLETED at the top of this function -- see the
            # `fenced` UPDATE above. Same transaction, same database --
            # consuming the usage
            # reservation here (rather than a separate call after
            # commit) is what actually makes "quota consumed exactly
            # once" atomic with "result persisted", not just
            # sequential and hopeful.
            await conn.execute(
                "UPDATE analysis_usage SET status = 'CONSUMED', consumed_at = now() "
                "WHERE id = $1 AND status = 'RESERVED'",
                usage_reservation_id,
            )
