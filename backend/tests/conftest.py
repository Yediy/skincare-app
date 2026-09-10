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

import time
from pathlib import Path

import asyncpg
import psycopg2
import pytest
import pytest_asyncio
import redis as redis_sync_lib
import redis.asyncio as redis_lib
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Bounded connect/socket timeouts for every Redis client this test
# session creates -- see app/redis_client.py's init_redis for why an
# unbounded socket wait is the wrong default under load. 5s is
# generous for a real hiccup without ever masking a genuinely dead
# Redis for the whole test run.
_REDIS_TIMEOUT_SECONDS = 5.0


def _wait_for_test_infra_ready(timeout_seconds: float = 30.0) -> None:
    """
    Retries connecting to the test Postgres and Redis instances before
    the session's single migration run and every test after it. This
    is test-infrastructure setup only -- a bounded wait for services
    that are merely slow to accept connections yet (e.g. cold-started
    alongside this run) -- not a retry around real, later application
    behavior. Any timeout/connection error surviving past the deadline
    is re-raised as-is so a genuinely dead dependency still fails the
    session immediately and loudly, rather than being silently retried
    away.

    Deliberately synchronous (psycopg2/redis-py sync clients, not
    asyncpg/redis.asyncio): this runs from a plain, non-async,
    session-scoped fixture, invoked before pytest-asyncio has any
    event loop set up for the session. An earlier version of this
    function used `asyncio.run()` from that same sync fixture, which
    collided with pytest-asyncio's own loop depending on which test
    file got collected first (`RuntimeError: Runner.run() cannot be
    called from a running event loop`) -- a real bug this pass's own
    test run caught, not a hypothetical.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = psycopg2.connect(TEST_DATABASE_URL, connect_timeout=int(_REDIS_TIMEOUT_SECONDS))
            conn.close()
            r = redis_sync_lib.from_url(
                TEST_REDIS_URL,
                socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
                socket_timeout=_REDIS_TIMEOUT_SECONDS,
            )
            try:
                r.ping()
            finally:
                r.close()
            return
        except (OSError, ConnectionError, psycopg2.OperationalError, redis_sync_lib.RedisError) as e:
            last_error = e
            time.sleep(0.5)
    raise RuntimeError(
        f"Test Postgres/Redis not ready after {timeout_seconds}s"
    ) from last_error

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
    every test a real, correctly-shaped schema (not a hand-rolled one).

    Waits (bounded, test-infra-only) for Postgres and Redis to accept
    connections first -- a cold-started local Postgres/Redis can take
    a moment to come up, and without this the very first test to run
    would eat that delay as a real failure instead of test setup."""
    _wait_for_test_infra_ready()
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
async def clean_database(request):
    """Truncates all app tables before each test so tests don't leak
    state into each other, regardless of test order.

    Deliberately does not depend on `db_pool` as a normal fixture
    argument: that would create (and tear down) a real asyncpg pool
    for every single test in the suite, including tests that touch
    neither Postgres nor the app at all (pure CV math, config
    validation, etc.) -- infra churn and connection-pool contention
    those tests have no reason to pay for. Only tests that already
    declared a dependency on `db_pool`, `app_db_pool`, `client`, or
    `app_instance` (the only fixtures that can leave rows behind) pay
    for a truncate.

    Uses its own one-off `asyncpg.connect()`, not `request.
    getfixturevalue("db_pool")` -- dynamically resolving another
    *async* fixture from inside an already-running async fixture hit a
    real pytest-asyncio bug in this version (`RuntimeError: Runner.run()
    cannot be called from a running event loop`, caught by this pass's
    own test run before it ever reached CI), so this deliberately
    avoids that path rather than depending on `db_pool` as a normal
    argument (which would reintroduce the exact per-test pool churn
    this fixture exists to avoid)."""
    needs_db = bool(
        {"db_pool", "app_db_pool", "client", "app_instance"} & set(request.fixturenames)
    )
    if needs_db:
        conn = await asyncpg.connect(dsn=TEST_DATABASE_URL)
        try:
            await conn.execute(
                "TRUNCATE TABLE refresh_tokens, user_profiles, consent_events, jobs, analysis_usage, users RESTART IDENTITY CASCADE"
            )
        finally:
            await conn.close()
    yield


@pytest_asyncio.fixture
async def redis_client():
    client = redis_lib.from_url(
        TEST_REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
        socket_timeout=_REDIS_TIMEOUT_SECONDS,
    )
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest_asyncio.fixture
async def app_instance(db_pool, app_db_pool, redis_client):
    """The real FastAPI app, wired to the *restricted* app_db_pool
    (the skincare_app role, not the superuser db_pool) and test Redis
    client, instead of running its own startup/shutdown events (which
    would create a second, competing pool/client). `db_pool` is
    unused directly here (`clean_database` no longer depends on it --
    it opens its own one-off connection instead) but is kept as a
    dependency anyway: this exact fixture set is what the final,
    fully-green 114-test run (TEST_REPORT.md) verified, and this
    parameter costs nothing to keep."""
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


@pytest_asyncio.fixture
async def synthetic_catalog(db_pool, migrated_test_database):
    """Small, intentionally fictional catalog (fictional brand,
    fictional product names) covering every scenario
    PRODUCT_CATALOG_ARCHITECTURE.md's test-catalog phase requires:
    a basic-moisturizer SAFE case, a fragrance/allergen case, a
    pregnancy/nursing-restricted retinoid case, a sensitive-skin acid
    case, an ingredient-interaction conflict, and an incomplete
    (INSUFFICIENT_DATA) formulation. Seeded through db_pool (the
    superuser/migration-owner connection) -- skincare_app has
    SELECT-only grants on every catalog table by design (migrations
    d70e5fc90775/16b82dde6e7d), so it cannot seed its own test data,
    matching the same test-owner-seeds/runtime-role-reads split
    app/db/usage_repository.py and every other RLS-protected table in
    this repository already uses.

    Self-contained: truncates every catalog table itself before
    seeding, rather than relying on the (unrelated) clean_database
    fixture, so this fixture's own seeded IDs are always the only rows
    present regardless of test order or which other fixtures ran.

    Returns a dict of the UUIDs tests need to reference specific
    ingredients/formulations without re-deriving them by name.
    """
    async with db_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE ingredient_interactions, ingredient_rules, formulation_ingredients, "
            "ingredient_aliases, ingredients, product_skus, product_formulations, products, brands CASCADE"
        )

        brand_id = await conn.fetchval(
            "INSERT INTO brands (name, normalized_name) VALUES ($1, $2) RETURNING id",
            "Testonyx", "testonyx",
        )

        async def _ingredient(canonical_name, ingredient_type, inci_name=None, aliases=()):
            ingredient_id = await conn.fetchval(
                "INSERT INTO ingredients (canonical_name, normalized_name, inci_name, ingredient_type) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                canonical_name, canonical_name.strip().lower(), inci_name, ingredient_type,
            )
            for alias, alias_type in aliases:
                await conn.execute(
                    "INSERT INTO ingredient_aliases (ingredient_id, alias, normalized_alias, alias_type) "
                    "VALUES ($1, $2, $3, $4)",
                    ingredient_id, alias, alias.strip().lower(), alias_type,
                )
            return ingredient_id

        water_id = await _ingredient("Water", "solvent")
        glycerin_id = await _ingredient("Glycerin", "humectant")
        dimethicone_id = await _ingredient("Dimethicone", "emollient")
        shea_butter_id = await _ingredient("Shea Butter", "emollient")
        fragrance_id = await _ingredient("Fragrance", "fragrance", aliases=[("Parfum", "synonym")])
        retinol_id = await _ingredient(
            "Retinol", "active", inci_name="Retinol", aliases=[("Vitamin A1", "common_name")]
        )
        glycolic_acid_id = await _ingredient(
            "Glycolic Acid", "active", aliases=[("AHA", "abbreviation"), ("Hydroxyacetic Acid", "synonym")]
        )
        niacinamide_id = await _ingredient("Niacinamide", "active", aliases=[("Vitamin B3", "common_name")])

        async def _product_with_formulation(name, category, ingredient_ids, sku):
            product_id = await conn.fetchval(
                "INSERT INTO products (brand_id, name, normalized_name, category) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                brand_id, name, name.strip().lower(), category,
            )
            formulation_id = await conn.fetchval(
                """
                INSERT INTO product_formulations (product_id, version, source_type, verified_at)
                VALUES ($1, '1', 'manufacturer_disclosure', now())
                RETURNING id
                """,
                product_id,
            )
            await conn.execute(
                "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, $3)",
                product_id, formulation_id, sku,
            )
            for position, ingredient_id in enumerate(ingredient_ids, start=1):
                await conn.execute(
                    "INSERT INTO formulation_ingredients (formulation_id, ingredient_id, position) "
                    "VALUES ($1, $2, $3)",
                    formulation_id, ingredient_id, position,
                )
            return product_id, formulation_id

        moisturizer_product_id, moisturizer_formulation_id = await _product_with_formulation(
            "Basic Gentle Moisturizer", "moisturizer",
            [water_id, glycerin_id, dimethicone_id, shea_butter_id], "TNX-MOIST-01",
        )
        fragrance_product_id, fragrance_formulation_id = await _product_with_formulation(
            "Soft Bloom Fragrance Cream", "moisturizer",
            [water_id, glycerin_id, fragrance_id], "TNX-FRAG-01",
        )
        retinol_product_id, retinol_formulation_id = await _product_with_formulation(
            "Renewal Night Retinol Serum", "retinoid",
            [water_id, retinol_id], "TNX-RETIN-01",
        )
        acid_product_id, acid_formulation_id = await _product_with_formulation(
            "Glow Peel Exfoliating Toner", "chemical_exfoliant",
            [water_id, glycolic_acid_id], "TNX-ACID-01",
        )
        combo_product_id, combo_formulation_id = await _product_with_formulation(
            "Dual Active Renewal Complex", "retinoid",
            [water_id, retinol_id, glycolic_acid_id], "TNX-COMBO-01",
        )
        safe_serum_product_id, safe_serum_formulation_id = await _product_with_formulation(
            "Bright Even Niacinamide Serum", "niacinamide_serum",
            [water_id, niacinamide_id], "TNX-NIA-01",
        )

        # A second brand/product deliberately pointing at the SAME
        # formulation as the retinol serum above (a common real-world
        # pattern -- white-label/private-label reformulation-free
        # rebrand) -- proves changing brand/product/SKU display text
        # cannot change the safety result for an unchanged formulation.
        rebrand_id = await conn.fetchval(
            "INSERT INTO brands (name, normalized_name) VALUES ($1, $2) RETURNING id",
            "Rebrand Labs", "rebrand labs",
        )
        rebrand_product_id = await conn.fetchval(
            "INSERT INTO products (brand_id, name, normalized_name, category) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            rebrand_id, "Premium Renewal Serum", "premium renewal serum", "retinoid",
        )
        await conn.execute(
            "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, $3)",
            rebrand_product_id, retinol_formulation_id, "REBRAND-01",
        )

        # Incomplete formulation: a real product/formulation row exists,
        # but with zero declared ingredients -- INSUFFICIENT_DATA, not
        # SAFE (this pass's Unknown Formulation Policy).
        incomplete_product_id, incomplete_formulation_id = await _product_with_formulation(
            "Undisclosed Mystery Serum", "vitamin_c_serum", [], "TNX-MYSTERY-01",
        )

        # Ingredient-level rules: pregnancy/nursing exclusion on
        # retinol, sensitive-skin restriction on glycolic acid.
        await conn.execute(
            "INSERT INTO ingredient_rules (ingredient_id, rule_type, severity, action, reason_code) "
            "VALUES ($1, 'PREGNANCY', 'HIGH', 'EXCLUDE', 'PREGNANCY_RESTRICTION')",
            retinol_id,
        )
        await conn.execute(
            "INSERT INTO ingredient_rules (ingredient_id, rule_type, severity, action, reason_code) "
            "VALUES ($1, 'NURSING', 'HIGH', 'EXCLUDE', 'NURSING_RESTRICTION')",
            retinol_id,
        )
        await conn.execute(
            "INSERT INTO ingredient_rules (ingredient_id, rule_type, severity, action, reason_code) "
            "VALUES ($1, 'SENSITIVE_SKIN', 'MODERATE', 'RESTRICT', 'SENSITIVE_SKIN_INTENSITY_LIMIT')",
            glycolic_acid_id,
        )

        # Pairwise interaction: retinol + glycolic acid together, high
        # severity -- exercised by the "Dual Active" formulation above.
        a_id, b_id = (retinol_id, glycolic_acid_id) if str(retinol_id) < str(glycolic_acid_id) else (glycolic_acid_id, retinol_id)
        await conn.execute(
            """
            INSERT INTO ingredient_interactions
                (ingredient_a_id, ingredient_b_id, interaction_type, severity, reason_code, recommendation)
            VALUES ($1, $2, 'INCOMPATIBLE', 'HIGH', 'ACTIVE_INTERACTION_CONFLICT',
                    'Alternate nights rather than applying together to avoid over-exfoliation/irritation.')
            """,
            a_id, b_id,
        )

    return {
        "brand_id": brand_id,
        "rebrand_id": rebrand_id,
        "ingredients": {
            "water": water_id, "glycerin": glycerin_id, "dimethicone": dimethicone_id,
            "shea_butter": shea_butter_id, "fragrance": fragrance_id, "retinol": retinol_id,
            "glycolic_acid": glycolic_acid_id, "niacinamide": niacinamide_id,
        },
        "products": {
            "moisturizer": moisturizer_product_id, "fragrance": fragrance_product_id,
            "retinol": retinol_product_id, "acid": acid_product_id, "combo": combo_product_id,
            "safe_serum": safe_serum_product_id, "rebrand": rebrand_product_id,
            "incomplete": incomplete_product_id,
        },
        "formulations": {
            "moisturizer": moisturizer_formulation_id, "fragrance": fragrance_formulation_id,
            "retinol": retinol_formulation_id, "acid": acid_formulation_id, "combo": combo_formulation_id,
            "safe_serum": safe_serum_formulation_id, "incomplete": incomplete_formulation_id,
        },
    }
