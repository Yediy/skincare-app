"""
Test configuration. Points every test at dedicated, isolated test
infrastructure -- never at the dev/prod database or Redis index used by
a manually-run server:

- Postgres: a separate database ("skincare_test"), not "skincare".
- Redis: a separate logical DB index (1), not the app's default (0).

These env vars are set at import time, before any `app.*` module is
imported anywhere in the test session -- app.config.settings is a
module-level singleton, so this must happen first.
"""
import os

os.environ["DATABASE_URL"] = "postgresql://postgres:postgres@localhost:5432/skincare_test"
os.environ["REDIS_URL"] = "redis://localhost:6379/1"
os.environ.setdefault("JWT_SECRET", "test-only-secret-never-used-outside-pytest-0123456789abcdef")

import asyncio
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import redis.asyncio as redis_lib
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Superuser -- test harness use only (setup, assertions, TRUNCATE
# cleanup, running migrations). The application itself never connects
# this way; see APP_DATABASE_URL / app_db_pool below for what the
# injected FastAPI app actually queries through.
TEST_DATABASE_URL = os.environ["DATABASE_URL"]
# The restricted, non-superuser role every real request goes through
# (see migration 7b38b717546e). Using this, not TEST_DATABASE_URL, for
# app_instance is what actually proves the app works correctly under
# the same restricted role production uses, RLS included.
APP_DATABASE_URL = "postgresql://skincare_app:skincare_app_dev_only@localhost:5432/skincare_test"
TEST_REDIS_URL = os.environ["REDIS_URL"]


def _run_migrations_to_head():
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database():
    """Runs the real Alembic migration chain against the test database
    once per test session -- proves migrations reach head, and gives
    every test a real, correctly-shaped schema (not a hand-rolled one)."""
    _run_migrations_to_head()
    yield


@pytest_asyncio.fixture
async def db_pool(migrated_test_database):
    pool = await asyncpg.create_pool(dsn=TEST_DATABASE_URL, min_size=1, max_size=5)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def app_db_pool(migrated_test_database):
    """The restricted skincare_app role's own pool -- separate from
    db_pool (superuser) so tests can prove the app genuinely works,
    and is genuinely isolated by RLS, through the same restricted
    connection production uses."""
    pool = await asyncpg.create_pool(dsn=APP_DATABASE_URL, min_size=1, max_size=5)
    yield pool
    await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_database(db_pool):
    """Truncates all app tables before each test so tests don't leak
    state into each other, regardless of test order."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE refresh_tokens, user_profiles, consent_events, users RESTART IDENTITY CASCADE"
        )
    yield


@pytest_asyncio.fixture
async def redis_client():
    client = redis_lib.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest_asyncio.fixture
async def app_instance(db_pool, app_db_pool, redis_client):
    """The real FastAPI app, wired to the *restricted* app_db_pool
    (the skincare_app role, not the superuser db_pool) and test Redis
    client, instead of running its own startup/shutdown events (which
    would create a second, competing pool/client). db_pool is still
    depended on here so clean_database (which uses it) always runs
    before app_instance is used."""
    from app import main as main_module

    main_module.app.dependency_overrides.clear()
    from app.db import connection as db_connection
    from app import redis_client as redis_module

    db_connection._pool = app_db_pool
    redis_module._redis_client = redis_client

    yield main_module.app

    db_connection._pool = None
    redis_module._redis_client = None


@pytest_asyncio.fixture
async def client(app_instance):
    # raise_app_exceptions=False: let unhandled exceptions become real
    # 500 responses via Starlette's error middleware, matching what a
    # real deployed server does -- the default (True) instead re-raises
    # them into the test itself, which would make it impossible to
    # assert on 500 behavior at all.
    transport = ASGITransport(app=app_instance, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
