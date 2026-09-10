"""Real PostgreSQL-level restricted-role tests for the product/usage
foundation pass's new tables (migrations d70e5fc90775, 16b82dde6e7d,
ee276e90a60f) -- run through the actual restricted skincare_app role
(app_db_pool), not mocked and not run as the superuser, matching the
existing pattern in test_rls_isolation.py.

Two different things are being proven here, for two different reasons:
- analysis_usage is user-owned data -- cross-user RLS isolation, same
  as user_profiles/consent_events.
- The catalog tables (brands/products/.../ingredient_interactions) are
  global reference data with NO RLS -- what's being proven instead is
  that skincare_app genuinely cannot write to them at all (catalog
  read vs. catalog administration are separate roles/grants, not
  merely a convention), while still being able to read them.
"""
import uuid

import asyncpg
import pytest

from app.db import usage_repository


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id", email
    )
    return row["id"]


async def test_user_a_cannot_read_user_b_analysis_usage(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "usage-rls-a@test.com")
    user_b = await _create_user(db_pool, "usage-rls-b@test.com")

    await usage_repository.reserve(app_db_pool, user_b, str(uuid.uuid4()), "2026-09", allowance=5)

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            rows = await conn.fetch("SELECT * FROM analysis_usage WHERE user_id = $1", user_b)

    assert rows == []


async def test_user_a_cannot_release_user_bs_reservation(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "usage-rls-release-a@test.com")
    user_b = await _create_user(db_pool, "usage-rls-release-b@test.com")

    reservation = await usage_repository.reserve(app_db_pool, user_b, str(uuid.uuid4()), "2026-09", allowance=5)

    # release() itself always scopes by the user_id it's called with
    # (app/db/usage_repository.py sets app.current_user_id from its
    # own argument) -- the real, structural proof RLS adds on top of
    # that is a *direct* query as user A explicitly targeting B's row
    # by ID, which must still affect zero rows regardless of what the
    # WHERE clause asks for.
    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            result = await conn.execute(
                "UPDATE analysis_usage SET status = 'RELEASED' WHERE id = $1", reservation.id
            )

    assert result.endswith(" 0")
    b_row = await db_pool.fetchrow("SELECT status FROM analysis_usage WHERE id = $1", reservation.id)
    assert b_row["status"] == "RESERVED"  # unchanged


async def test_skincare_app_cannot_insert_into_catalog_tables(app_db_pool):
    """Catalog write access belongs to migrations/catalog-administration
    only -- the runtime API role must not be able to rewrite ingredient
    safety data merely because some HTTP route's connection pool
    happens to be this role."""
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with app_db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO brands (name, normalized_name) VALUES ('Hack Co', 'hack co')"
            )


async def test_skincare_app_cannot_insert_ingredient_rules(app_db_pool, synthetic_catalog):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with app_db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ingredient_rules (ingredient_id, rule_type, severity, action, reason_code) "
                "VALUES ($1, 'PREGNANCY', 'LOW', 'WARN', 'X')",
                synthetic_catalog["ingredients"]["water"],
            )


async def test_skincare_app_can_read_catalog_tables(app_db_pool, synthetic_catalog):
    async with app_db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT canonical_name FROM ingredients WHERE id = $1", synthetic_catalog["ingredients"]["retinol"]
        )
    assert row["canonical_name"] == "Retinol"
