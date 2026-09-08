"""Phase 17: real PostgreSQL-level cross-user isolation tests, run
against the actual restricted skincare_app role (app_db_pool), not
mocked and not run as the superuser. Covers user_profiles and
consent_events -- the two tables RLS is enabled on (see migration
7b38b717546e for why users/refresh_tokens are explicitly out of scope
for this pass).
"""
import uuid

import pytest


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id",
        email,
    )
    return row["id"]


async def test_user_a_cannot_read_user_b_profile(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "rls-a@test.com")
    user_b = await _create_user(db_pool, "rls-b@test.com")

    from app.db.profile_repository import upsert_profile, DEFAULT_PROFILE

    b_profile = dict(DEFAULT_PROFILE)
    b_profile["experience_level"] = "advanced"
    await upsert_profile(app_db_pool, user_b, b_profile)

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            # Explicitly asking for B's row by ID, as A -- RLS must
            # still return nothing, regardless of what the WHERE
            # clause itself says.
            row = await conn.fetchrow("SELECT * FROM user_profiles WHERE user_id = $1", user_b)

    assert row is None


async def test_user_a_cannot_update_user_b_profile(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "rls-upd-a@test.com")
    user_b = await _create_user(db_pool, "rls-upd-b@test.com")

    from app.db.profile_repository import upsert_profile, DEFAULT_PROFILE

    await upsert_profile(app_db_pool, user_b, dict(DEFAULT_PROFILE))

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            result = await conn.execute(
                "UPDATE user_profiles SET has_sensitive_skin = true WHERE user_id = $1", user_b
            )

    assert result.endswith(" 0")  # zero rows affected

    b_row = await db_pool.fetchrow("SELECT has_sensitive_skin FROM user_profiles WHERE user_id = $1", user_b)
    assert b_row["has_sensitive_skin"] is False  # unchanged


async def test_user_a_cannot_delete_user_b_consent_event(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "rls-del-a@test.com")
    user_b = await _create_user(db_pool, "rls-del-b@test.com")

    from app.db.consent_repository import record_consent

    await record_consent(app_db_pool, user_b, "facial_analysis", "1.0", "testing")

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            result = await conn.execute("DELETE FROM consent_events WHERE user_id = $1", user_b)

    assert result.endswith(" 0")

    b_count = await db_pool.fetchval("SELECT count(*) FROM consent_events WHERE user_id = $1", user_b)
    assert b_count == 1  # B's row survived A's delete attempt


async def test_pooled_connection_reuse_does_not_leak_context_between_users(db_pool, app_db_pool):
    """The core promise of using SET LOCAL (via set_config's third
    arg): context is scoped to one transaction, not the underlying
    connection. Proven here on a single, explicitly reused connection
    -- not two different pool.acquire() calls that might already land
    on different physical connections."""
    user_a = await _create_user(db_pool, "rls-reuse-a@test.com")
    user_b = await _create_user(db_pool, "rls-reuse-b@test.com")

    from app.db.profile_repository import upsert_profile, DEFAULT_PROFILE

    a_profile = dict(DEFAULT_PROFILE)
    a_profile["experience_level"] = "advanced"
    await upsert_profile(app_db_pool, user_a, a_profile)

    async with app_db_pool.acquire() as conn:
        # Transaction 1: context set to A, A's row correctly visible.
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            row = await conn.fetchrow("SELECT experience_level FROM user_profiles WHERE user_id = $1", user_a)
            assert row["experience_level"] == "advanced"

        # Transaction 2: same physical connection, no new context set.
        # If SET LOCAL's scoping didn't work, this might still see A's
        # context (real leak). It must instead see nothing at all --
        # current_setting() returns NULL post-commit, which the RLS
        # policy correctly treats as "no identity", not "still A".
        async with conn.transaction():
            leaked = await conn.fetchrow(
                "SELECT experience_level FROM user_profiles WHERE user_id = $1", user_a
            )
            assert leaked is None

        # Transaction 3: now correctly scoped to B on the SAME reused
        # connection -- proves the connection itself isn't broken,
        # just that context doesn't persist across transactions.
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_b))
            b_row = await conn.fetchrow("SELECT user_id FROM user_profiles WHERE user_id = $1", user_a)
            assert b_row is None  # B's context still can't see A's row
