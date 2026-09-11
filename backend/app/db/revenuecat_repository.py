"""Persistence for RevenueCat billing (migration a1c9f3e7b2d4). See
BILLING_ARCHITECTURE.md for the end-to-end flow this sits in.

record_event() is the idempotent durable-receipt half of "verify ->
durable event insert -> enqueue -> 200": ON CONFLICT (revenuecat_event_id)
DO NOTHING then fetch-if-absent, the same pattern
PostgresJobQueue.enqueue()/analysis_repository.create_request() already
use for exactly the same reason -- N concurrent callers with the same
natural key must produce exactly one durable row.

apply_entitlement_projection() is the out-of-order-safe write to
user_entitlements: a single INSERT ... ON CONFLICT ... DO UPDATE ...
WHERE statement, not a read-then-compare-then-write sequence -- the
WHERE clause on the DO UPDATE makes a stale write (incoming
last_provider_event_at older than what's already stored) a genuine
no-op at the database level, safe under concurrent processing of two
events for the same (user, entitlement, provider, environment) without
any extra locking. See REVENUECAT_INTEGRATION_NOTES.md section 3 for
why "stale must not overwrite newer" matters here.

Every entitlement read/write sets app.current_user_id to the row's own
user_id first, same convention as usage_repository.py/
analysis_repository.py -- RLS (migration a1c9f3e7b2d4) scopes every
statement to that GUC.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg

PENDING = "PENDING"
PROCESSED = "PROCESSED"
STALE_IGNORED = "STALE_IGNORED"
FAILED = "FAILED"
# Durably received and verified, but this pass cannot safely apply it
# without more information -- a TRANSFER with no resolvable
# environment, zero resolvable local destination users, or more than
# one distinct resolvable local destination user. Never retried
# automatically (it isn't an exception), never silently dropped
# either -- see app/domain/revenuecat_entitlement_processor.py and
# migration 9815eb266923.
RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
# Durably received, the app_user_id resolved fine, but the event's own
# `entitlement_ids` do not include settings.revenuecat_entitlement_id
# -- an unrelated RevenueCat product/entitlement. A normal, successful
# outcome, not an error.
NOT_RELEVANT = "NOT_RELEVANT"

ACTIVE = "ACTIVE"
GRACE_PERIOD = "GRACE_PERIOD"
EXPIRED = "EXPIRED"
REVOKED = "REVOKED"


@dataclass(frozen=True)
class RecordedEvent:
    id: UUID
    is_new: bool
    processing_status: str


async def record_event(
    pool: asyncpg.Pool,
    *,
    revenuecat_event_id: str,
    event_type: str,
    app_user_id: Optional[str],
    environment: Optional[str],
    event_timestamp: datetime,
    payload_json: Dict[str, Any],
    conn: Optional[asyncpg.Connection] = None,
) -> RecordedEvent:
    import json

    async def _run(c: asyncpg.Connection) -> RecordedEvent:
        row = await c.fetchrow(
            """
            INSERT INTO revenuecat_webhook_events
                (revenuecat_event_id, event_type, app_user_id, environment, event_timestamp, payload_json)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (revenuecat_event_id) DO NOTHING
            RETURNING id, processing_status
            """,
            revenuecat_event_id, event_type, app_user_id, environment, event_timestamp, json.dumps(payload_json),
        )
        if row is not None:
            return RecordedEvent(id=row["id"], is_new=True, processing_status=row["processing_status"])

        existing = await c.fetchrow(
            "SELECT id, processing_status FROM revenuecat_webhook_events WHERE revenuecat_event_id = $1",
            revenuecat_event_id,
        )
        return RecordedEvent(id=existing["id"], is_new=False, processing_status=existing["processing_status"])

    if conn is not None:
        return await _run(conn)
    async with pool.acquire() as acquired:
        return await _run(acquired)


async def get_event(pool: asyncpg.Pool, webhook_event_id: UUID) -> Optional[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM revenuecat_webhook_events WHERE id = $1", webhook_event_id
        )


async def mark_event_result(
    pool: asyncpg.Pool,
    webhook_event_id: UUID,
    *,
    status: str,
    error_code: Optional[str] = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE revenuecat_webhook_events
            SET processing_status = $2, processed_at = now(),
                attempt_count = attempt_count + 1, last_error_code = $3
            WHERE id = $1
            """,
            webhook_event_id, status, error_code,
        )


@dataclass(frozen=True)
class EntitlementProjection:
    applied: bool  # False when the incoming event was stale relative to stored state
    status: Optional[str] = None


async def apply_entitlement_projection(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    entitlement_identifier: str,
    provider: str,
    status: str,
    effective_at: datetime,
    expires_at: Optional[datetime],
    will_renew: bool,
    environment: str,
    source_event_id: Optional[str],
    provider_customer_id: str,
    last_provider_event_at: datetime,
) -> EntitlementProjection:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            row = await conn.fetchrow(
                """
                INSERT INTO user_entitlements
                    (user_id, entitlement_identifier, provider, status, effective_at, expires_at,
                     will_renew, environment, source_event_id, provider_customer_id, last_provider_event_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (user_id, entitlement_identifier, provider, environment)
                DO UPDATE SET
                    status = EXCLUDED.status,
                    effective_at = EXCLUDED.effective_at,
                    expires_at = EXCLUDED.expires_at,
                    will_renew = EXCLUDED.will_renew,
                    source_event_id = EXCLUDED.source_event_id,
                    provider_customer_id = EXCLUDED.provider_customer_id,
                    last_provider_event_at = EXCLUDED.last_provider_event_at,
                    updated_at = now()
                WHERE user_entitlements.last_provider_event_at <= EXCLUDED.last_provider_event_at
                RETURNING id, status
                """,
                user_id, entitlement_identifier, provider, status, effective_at, expires_at,
                will_renew, environment, source_event_id, provider_customer_id, last_provider_event_at,
            )
    if row is None:
        return EntitlementProjection(applied=False)
    return EntitlementProjection(applied=True, status=row["status"])


async def get_entitlement(
    pool: asyncpg.Pool,
    user_id: UUID,
    entitlement_identifier: str,
    provider: str,
    environment: str,
) -> Optional[asyncpg.Record]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_id))
            return await conn.fetchrow(
                """
                SELECT * FROM user_entitlements
                WHERE user_id = $1 AND entitlement_identifier = $2 AND provider = $3 AND environment = $4
                """,
                user_id, entitlement_identifier, provider, environment,
            )
