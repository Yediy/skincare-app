"""Real Postgres-level tests for migration a1c9f3e7b2d4/9815eb266923's
RLS posture on user_entitlements, exercised through the dedicated
`skincare_billing` role -- the only role with any write grant on this
table (see tests/database/test_revenuecat_billing_privilege.py for the
"the ordinary skincare_app role cannot write at all" half of the
privilege boundary). Proves RLS itself: (1) cross-user writes are
blocked regardless of what a query's WHERE clause claims, even for the
privileged billing role, and (2) attributing a write to a webhook
event that was never durably received is blocked by a real foreign
key, not merely a convention.
"""
from datetime import datetime, timezone

import asyncpg
import pytest

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


async def test_billing_role_cannot_write_a_row_scoped_to_a_different_current_user(db_pool, billing_db_pool):
    """RLS applies even to the privileged billing role -- it is not
    BYPASSRLS. Setting app.current_user_id to user_a but attempting to
    insert a row whose own user_id column is user_b is blocked."""
    user_a = await _create_user(db_pool, "rc-rls-a@test.com")
    user_b = await _create_user(db_pool, "rc-rls-b@test.com")

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with billing_db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
                await conn.execute(
                    """
                    INSERT INTO user_entitlements
                        (user_id, entitlement_identifier, provider, status, effective_at,
                         environment, provider_customer_id, last_provider_event_at)
                    VALUES ($1, 'premium', 'revenuecat', 'ACTIVE', now(), 'PRODUCTION', $2, now())
                    """,
                    user_b, str(user_b),
                )


async def test_user_a_cannot_read_user_bs_entitlement_row(db_pool, app_db_pool, billing_db_pool):
    from app.db import revenuecat_repository

    user_a = await _create_user(db_pool, "rc-rls-read-a@test.com")
    user_b = await _create_user(db_pool, "rc-rls-read-b@test.com")

    await revenuecat_repository.apply_entitlement_projection(
        billing_db_pool, user_id=user_b, entitlement_identifier="premium", provider="revenuecat",
        status="ACTIVE", effective_at=T0, expires_at=None, will_renew=True, environment="PRODUCTION",
        source_event_id=None, provider_customer_id=str(user_b), last_provider_event_at=T0,
    )

    # Reads go through the ordinary app_db_pool (the same role
    # RevenueCatEntitlementService's read path uses) -- proves cross-
    # user isolation on the read side that matters in production, not
    # just on the billing role's own writes.
    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            rows = await conn.fetch("SELECT * FROM user_entitlements WHERE user_id = $1", user_b)

    assert rows == []


async def test_cannot_forge_entitlement_attributed_to_a_nonexistent_webhook_event(db_pool, billing_db_pool):
    """source_event_id has a real FK into revenuecat_webhook_events --
    fabricating a row that *claims* to come from a webhook event that
    was never durably received is rejected at the database level, even
    for the privileged billing role."""
    user_a = await _create_user(db_pool, "rc-rls-forge@test.com")

    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        async with billing_db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
                await conn.execute(
                    """
                    INSERT INTO user_entitlements
                        (user_id, entitlement_identifier, provider, status, effective_at,
                         environment, provider_customer_id, last_provider_event_at, source_event_id)
                    VALUES ($1, 'premium', 'revenuecat', 'ACTIVE', now(), 'PRODUCTION', $2, now(), $3)
                    """,
                    user_a, str(user_a), "fabricated-event-id-that-does-not-exist",
                )


async def test_billing_role_cannot_delete_entitlement_rows(db_pool, billing_db_pool):
    """No DELETE grant at all, for either role -- an entitlement
    transition supersedes the row, it is never erased."""
    from app.db import revenuecat_repository

    user_a = await _create_user(db_pool, "rc-rls-delete@test.com")
    await revenuecat_repository.apply_entitlement_projection(
        billing_db_pool, user_id=user_a, entitlement_identifier="premium", provider="revenuecat",
        status="ACTIVE", effective_at=T0, expires_at=None, will_renew=True, environment="PRODUCTION",
        source_event_id=None, provider_customer_id=str(user_a), last_provider_event_at=T0,
    )

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with billing_db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
                await conn.execute("DELETE FROM user_entitlements WHERE user_id = $1", user_a)
