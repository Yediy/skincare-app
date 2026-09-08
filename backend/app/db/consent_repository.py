"""Append-only consent ledger.

Granting consent always INSERTs a new row -- a prior grant's row is
never modified to reflect a re-consent or a policy change (that's a
separate new row). The one narrow exception is `withdrawn_at`: a
withdrawal sets that single column on the currently-active row for a
user+consent_type pair, rather than deleting or rewriting the historical
grant itself. That is the only mutation this module ever performs.

REQUIRED_CONSENT_TYPE / REQUIRED_POLICY_VERSION are the server's own
single source of truth for what's currently required -- callers (this
API, and eventually a mobile client) must defer to these values rather
than hardcoding their own independent copy.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg

REQUIRED_CONSENT_TYPE = "facial_analysis"
REQUIRED_POLICY_VERSION = "1.0"


async def record_consent(
    pool: asyncpg.Pool,
    user_id: UUID,
    consent_type: str,
    policy_version: str,
    purpose: str,
    jurisdiction: Optional[str] = None,
    app_version: Optional[str] = None,
    platform: Optional[str] = None,
) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO consent_events
                (user_id, consent_type, policy_version, purpose, jurisdiction,
                 granted_at, app_version, platform)
            VALUES ($1, $2, $3, $4, $5, now(), $6, $7)
            RETURNING id, granted_at
            """,
            user_id, consent_type, policy_version, purpose, jurisdiction,
            app_version, platform,
        )
    return {"id": str(row["id"]), "granted_at": row["granted_at"].isoformat()}


async def withdraw_consent(pool: asyncpg.Pool, user_id: UUID, consent_type: str) -> bool:
    """Withdraws the currently-active grant (if any) for this
    user+consent_type. Returns whether an active grant existed to
    withdraw."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE consent_events SET withdrawn_at = now()
            WHERE id = (
                SELECT id FROM consent_events
                WHERE user_id = $1 AND consent_type = $2 AND withdrawn_at IS NULL
                ORDER BY granted_at DESC
                LIMIT 1
            )
            """,
            user_id, consent_type,
        )
    return result.endswith(" 1")


async def has_valid_consent(
    pool: asyncpg.Pool,
    user_id: UUID,
    consent_type: str,
    required_policy_version: str,
) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM consent_events
            WHERE user_id = $1 AND consent_type = $2 AND policy_version = $3
              AND withdrawn_at IS NULL
            ORDER BY granted_at DESC
            LIMIT 1
            """,
            user_id, consent_type, required_policy_version,
        )
    return row is not None
