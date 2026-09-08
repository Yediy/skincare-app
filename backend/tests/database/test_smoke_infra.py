"""Smoke tests proving the database and Redis connections can actually
initialize against the test infrastructure, and that Alembic migrations
reach head -- these are Phase 1's required infrastructure proofs, not
placeholders."""
import asyncpg

from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL


async def test_database_connection_can_initialize():
    conn = await asyncpg.connect(dsn=TEST_DATABASE_URL)
    try:
        result = await conn.fetchval("SELECT 1")
        assert result == 1
    finally:
        await conn.close()


async def test_redis_connection_can_initialize(redis_client):
    assert await redis_client.ping() is True


async def test_migrations_reach_head(db_pool):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT version_num FROM alembic_version")
    assert row["version_num"] == "7b38b717546e"


async def test_all_expected_tables_exist(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'"
        )
    tables = {r["tablename"] for r in rows}
    assert {"users", "refresh_tokens", "alembic_version"}.issubset(tables)
