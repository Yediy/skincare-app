"""Migration 13c1fff1867e's core claim, proven through the real
restricted roles (never the superuser, never mocked): `skincare_catalog_
admin` can write everything this pass's ingestion pipeline needs
(staging/provenance/audit tables, plus the seven production catalog
tables) and NOTHING else -- not the ingredient safety-rule tables
(deliberately out of scope this pass), and not a single table this
brief explicitly calls out (users/billing/analysis/refresh_tokens).
`skincare_app` symmetrically has no write grant on any new staging
table (it already had none on the production catalog tables --
tests/database/test_product_usage_rls.py -- unaffected by this pass).
"""
import uuid

import asyncpg
import pytest


@pytest.fixture(autouse=True)
async def _clean(clean_catalog_ingestion):
    """Every test in this file gets a clean catalog/staging slate --
    several tests here use fixed, hardcoded names (e.g. "Priv Source"),
    so this file must not rely on unique-suffix-per-test naming the way
    some other catalog test files do."""


async def _create_brand_and_product(catalog_admin_db_pool, suffix):
    brand_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO brands (name, normalized_name) VALUES ($1, $2) RETURNING id",
        f"Priv Brand {suffix}", f"priv brand {suffix}",
    )
    product_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO products (brand_id, name, normalized_name, category) VALUES ($1, $2, $3, 'moisturizer') RETURNING id",
        brand_id, f"Priv Product {suffix}", f"priv product {suffix}",
    )
    return brand_id, product_id


# ---------------------------------------------------------------------------
# skincare_catalog_admin: can do everything this pipeline needs.
# ---------------------------------------------------------------------------


async def test_catalog_admin_can_insert_source(catalog_admin_db_pool):
    source_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_sources (name, normalized_name, source_type) VALUES ($1, $2, 'curated_dataset') RETURNING id",
        "Priv Source", "priv source",
    )
    assert source_id is not None


async def test_catalog_admin_can_insert_batch_and_record(catalog_admin_db_pool):
    source_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_sources (name, normalized_name, source_type) VALUES ($1, $2, 'curated_dataset') RETURNING id",
        "Priv Source Batch", "priv source batch",
    )
    batch_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_import_batches (source_id, content_sha256, records_total) "
        "VALUES ($1, $2, 1) RETURNING id",
        source_id, "a" * 64,
    )
    record_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_import_records (batch_id, external_record_id, raw_payload, payload_sha256) "
        "VALUES ($1, 'ext-1', '{}'::jsonb, $2) RETURNING id",
        batch_id, "b" * 64,
    )
    assert record_id is not None

    review_item_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_review_items (import_record_id, reason_code) VALUES ($1, 'UNKNOWN_INGREDIENT') RETURNING id",
        record_id,
    )
    assert review_item_id is not None

    await catalog_admin_db_pool.execute(
        "INSERT INTO catalog_audit_log (action, entity_type, entity_id, actor) VALUES ('IMPORT', 'catalog_import_batch', $1, 'test')",
        batch_id,
    )


async def test_catalog_admin_can_write_production_catalog_tables(catalog_admin_db_pool):
    brand_id, product_id = await _create_brand_and_product(catalog_admin_db_pool, uuid.uuid4().hex[:8])

    formulation_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO product_formulations (product_id, version, source_type) "
        "VALUES ($1, '1', 'manufacturer_disclosure') RETURNING id",
        product_id,
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, 'PRIV-SKU-1')",
        product_id, formulation_id,
    )
    ingredient_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name, ingredient_type) "
        "VALUES ($1, $2, 'active') RETURNING id",
        "Priv Ingredient", "priv ingredient",
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredient_aliases (ingredient_id, alias, normalized_alias) VALUES ($1, $2, $3)",
        ingredient_id, "Priv Alias", "priv alias",
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO formulation_ingredients (formulation_id, ingredient_id, position) VALUES ($1, $2, 1)",
        formulation_id, ingredient_id,
    )
    # UPDATE, not just INSERT.
    await catalog_admin_db_pool.execute(
        "UPDATE product_formulations SET publication_status = 'PUBLISHED' WHERE id = $1", formulation_id,
    )


async def test_catalog_admin_cannot_write_ingredient_rules(catalog_admin_db_pool):
    """Deliberately out of scope this pass -- Section 13."""
    ingredient_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ($1, $2) RETURNING id",
        "Rule Scope Ingredient", "rule scope ingredient",
    )
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(
            "INSERT INTO ingredient_rules (ingredient_id, rule_type, severity, action, reason_code) "
            "VALUES ($1, 'PREGNANCY', 'HIGH', 'EXCLUDE', 'X')",
            ingredient_id,
        )


async def test_catalog_admin_cannot_write_ingredient_interactions(catalog_admin_db_pool):
    a = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ($1, $2) RETURNING id", "IntA", "inta",
    )
    b = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ($1, $2) RETURNING id", "IntB", "intb",
    )
    a, b = (a, b) if str(a) < str(b) else (b, a)
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(
            "INSERT INTO ingredient_interactions (ingredient_a_id, ingredient_b_id, interaction_type, severity, reason_code) "
            "VALUES ($1, $2, 'INCOMPATIBLE', 'HIGH', 'X')",
            a, b,
        )


@pytest.mark.parametrize("table,columns,values", [
    ("users", "(email, password_hash)", "('catalog-priv-escalation@test.com', 'x')"),
    ("refresh_tokens", "(user_id, family_id, token_hash, expires_at)",
     "(gen_random_uuid(), gen_random_uuid(), 'x', now())"),
    ("consent_events", "(user_id, consent_type, policy_version, purpose, granted_at)",
     "(gen_random_uuid(), 'facial_analysis', '1', 'x', now())"),
    ("user_entitlements",
     "(user_id, entitlement_identifier, provider, status, effective_at, environment, provider_customer_id, last_provider_event_at)",
     "(gen_random_uuid(), 'premium', 'revenuecat', 'ACTIVE', now(), 'PRODUCTION', 'x', now())"),
    ("revenuecat_webhook_events", "(revenuecat_event_id, event_type, event_timestamp, payload_json)",
     "('x', 'INITIAL_PURCHASE', now(), '{}'::jsonb)"),
])
async def test_catalog_admin_cannot_write_unrelated_domain_tables(catalog_admin_db_pool, table, columns, values):
    """The brief's own explicit list: this role must gain no mutation
    access whatsoever to users/billing/analysis-domain tables."""
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(f"INSERT INTO {table} {columns} VALUES {values}")


async def test_catalog_admin_role_attributes(db_pool):
    row = await db_pool.fetchrow(
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
        "FROM pg_roles WHERE rolname = 'skincare_catalog_admin'"
    )
    assert row is not None
    assert row["rolcanlogin"] is False
    assert row["rolsuper"] is False
    assert row["rolcreatedb"] is False
    assert row["rolcreaterole"] is False
    assert row["rolbypassrls"] is False


async def test_catalog_admin_runtime_role_connects_and_inherits(catalog_admin_db_pool, db_pool):
    current_user = await catalog_admin_db_pool.fetchval("SELECT current_user")
    assert current_user == "skincare_catalog_runtime"
    is_member = await db_pool.fetchval(
        "SELECT pg_has_role('skincare_catalog_runtime', 'skincare_catalog_admin', 'MEMBER')"
    )
    assert is_member is True


# ---------------------------------------------------------------------------
# skincare_app: no write grant on any new staging table.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table,columns,values", [
    ("catalog_sources", "(name, normalized_name, source_type)", "('X', 'x', 'curated_dataset')"),
    ("catalog_import_batches", "(source_id, content_sha256, records_total)", "(gen_random_uuid(), 'a', 1)"),
    ("catalog_import_records", "(batch_id, external_record_id, raw_payload, payload_sha256)",
     "(gen_random_uuid(), 'e', '{}'::jsonb, 'b')"),
    ("catalog_review_items", "(import_record_id, reason_code)", "(gen_random_uuid(), 'UNKNOWN_INGREDIENT')"),
    ("catalog_formulation_provenance", "(formulation_id, import_record_id, verification_actor)",
     "(gen_random_uuid(), gen_random_uuid(), 'x')"),
    ("catalog_audit_log", "(action, entity_type, actor)", "('IMPORT', 'x', 'x')"),
])
async def test_app_role_cannot_write_catalog_staging_tables(app_db_pool, table, columns, values):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await app_db_pool.execute(f"INSERT INTO {table} {columns} VALUES {values}")


async def test_app_role_can_still_only_read_production_catalog_tables(app_db_pool, synthetic_catalog):
    """Unaffected by this pass -- re-proven here for locality with the
    rest of this file, alongside test_product_usage_rls.py's own
    equivalent assertion."""
    rows = await app_db_pool.fetch("SELECT id FROM product_formulations LIMIT 1")
    assert len(rows) == 1
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await app_db_pool.execute(
            "UPDATE product_formulations SET publication_status = 'PUBLISHED' WHERE id = $1", rows[0]["id"],
        )
