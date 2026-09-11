"""Smoke tests proving the database and Redis connections can actually
initialize against the test infrastructure, and that Alembic migrations
reach head -- these are Phase 1's required infrastructure proofs, not
placeholders."""
import asyncpg
import pytest

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
    assert row["version_num"] == "a1c9f3e7b2d4"


async def test_lifespan_initializes_and_closes_the_db_pool():
    """app.main.lifespan (replacing the deprecated on_event startup/
    shutdown handlers) must actually initialize the db pool/Redis
    client on entry and close the db pool on exit. Deliberately
    exercises the real lifespan context manager directly -- every
    other test's `app_instance` fixture bypasses it entirely (wiring
    db_connection._pool/redis_client._redis_client itself), so nothing
    else in this suite actually proves lifespan wiring works."""
    from app.main import app, lifespan
    from app.db import connection as db_connection
    from app import redis_client as redis_module

    assert db_connection._pool is None

    async with lifespan(app):
        pool = db_connection.get_db_pool()
        assert pool is not None
        assert await pool.fetchval("SELECT 1") == 1
        assert await redis_module.get_redis().ping() is True

    assert db_connection._pool is None
    with pytest.raises(RuntimeError):
        db_connection.get_db_pool()


async def test_all_expected_tables_exist(db_pool):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'"
        )
    tables = {r["tablename"] for r in rows}
    assert {
        "users", "refresh_tokens", "jobs", "alembic_version",
        "brands", "products", "product_formulations", "product_skus",
        "ingredients", "ingredient_aliases", "formulation_ingredients",
        "ingredient_rules", "ingredient_interactions", "analysis_usage",
    }.issubset(tables)
