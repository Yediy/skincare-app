"""Migration `e5277cf2f0ee`'s core claim, proven through the real
restricted `skincare_catalog_admin` role (never the superuser, never a
grant-inspection-only unit test): raw import evidence, the audit log,
and formulation provenance are immutable/append-only at the database
privilege level, not merely by application convention.
"""
import asyncpg
import pytest


@pytest.fixture(autouse=True)
async def _clean(clean_catalog_ingestion):
    pass


@pytest.fixture
async def source_id(catalog_admin_db_pool):
    return await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_sources (name, normalized_name, source_type) "
        "VALUES ('Immutability Source', 'immutability source', 'curated_dataset') RETURNING id"
    )


@pytest.fixture
async def batch_id(catalog_admin_db_pool, source_id):
    return await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_import_batches (source_id, content_sha256, records_total) "
        "VALUES ($1, $2, 1) RETURNING id",
        source_id, "a" * 64,
    )


@pytest.fixture
async def import_record_id(catalog_admin_db_pool, batch_id):
    return await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_import_records (batch_id, external_record_id, raw_payload, payload_sha256) "
        "VALUES ($1, 'ext-1', '{\"brand_name\": \"X\"}'::jsonb, $2) RETURNING id",
        batch_id, "b" * 64,
    )


# ---------------------------------------------------------------------------
# catalog_import_records: legitimate staging fields stay writable...
# ---------------------------------------------------------------------------


async def test_insert_raw_import_record_still_works(catalog_admin_db_pool, batch_id):
    record_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_import_records (batch_id, external_record_id, raw_payload, payload_sha256) "
        "VALUES ($1, 'ext-new', '{}'::jsonb, $2) RETURNING id",
        batch_id, "c" * 64,
    )
    assert record_id is not None


async def test_update_status_still_works(catalog_admin_db_pool, import_record_id):
    await catalog_admin_db_pool.execute(
        "UPDATE catalog_import_records SET status = 'NORMALIZED' WHERE id = $1", import_record_id,
    )
    row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record_id,
    )
    assert row["status"] == "NORMALIZED"


async def test_update_normalized_payload_still_works(catalog_admin_db_pool, import_record_id):
    await catalog_admin_db_pool.execute(
        "UPDATE catalog_import_records SET normalized_payload = '{\"ok\": true}'::jsonb WHERE id = $1",
        import_record_id,
    )


async def test_update_validation_errors_still_works(catalog_admin_db_pool, import_record_id):
    await catalog_admin_db_pool.execute(
        "UPDATE catalog_import_records SET validation_errors = '[\"X\"]'::jsonb WHERE id = $1", import_record_id,
    )


async def test_update_review_reason_codes_still_works(catalog_admin_db_pool, import_record_id):
    await catalog_admin_db_pool.execute(
        "UPDATE catalog_import_records SET review_reason_codes = '[\"UNKNOWN_INGREDIENT\"]'::jsonb WHERE id = $1",
        import_record_id,
    )


async def test_update_formulation_id_and_superseded_formulation_id_still_work(
    catalog_admin_db_pool, import_record_id,
):
    await catalog_admin_db_pool.execute(
        "UPDATE catalog_import_records SET formulation_id = NULL, superseded_formulation_id = NULL WHERE id = $1",
        import_record_id,
    )


async def test_update_updated_at_still_works(catalog_admin_db_pool, import_record_id):
    await catalog_admin_db_pool.execute(
        "UPDATE catalog_import_records SET updated_at = now() WHERE id = $1", import_record_id,
    )


# ---------------------------------------------------------------------------
# ...but the immutable evidence columns are genuinely unwritable.
# ---------------------------------------------------------------------------


async def test_update_raw_payload_denied(catalog_admin_db_pool, import_record_id):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(
            "UPDATE catalog_import_records SET raw_payload = '{\"tampered\": true}'::jsonb WHERE id = $1",
            import_record_id,
        )


async def test_update_payload_sha256_denied(catalog_admin_db_pool, import_record_id):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(
            "UPDATE catalog_import_records SET payload_sha256 = 'f' || repeat('0', 63) WHERE id = $1",
            import_record_id,
        )


async def test_update_external_record_id_denied(catalog_admin_db_pool, import_record_id):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(
            "UPDATE catalog_import_records SET external_record_id = 'renamed' WHERE id = $1", import_record_id,
        )


async def test_update_batch_id_denied(catalog_admin_db_pool, import_record_id, source_id):
    other_batch_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_import_batches (source_id, content_sha256, records_total) "
        "VALUES ($1, $2, 1) RETURNING id",
        source_id, "d" * 64,
    )
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(
            "UPDATE catalog_import_records SET batch_id = $2 WHERE id = $1", import_record_id, other_batch_id,
        )


async def test_update_created_at_denied(catalog_admin_db_pool, import_record_id):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(
            "UPDATE catalog_import_records SET created_at = now() WHERE id = $1", import_record_id,
        )


async def test_update_denied_even_when_also_touching_a_legitimate_column(catalog_admin_db_pool, import_record_id):
    """A single statement that touches one forbidden column alongside
    otherwise-legitimate ones must fail as a whole -- never a partial
    grant that silently drops the disallowed assignment."""
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(
            "UPDATE catalog_import_records SET status = 'REJECTED', raw_payload = '{}'::jsonb WHERE id = $1",
            import_record_id,
        )
    row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record_id,
    )
    assert row["status"] != "REJECTED"


# ---------------------------------------------------------------------------
# catalog_audit_log: append-only.
# ---------------------------------------------------------------------------


async def test_insert_audit_row_still_works(catalog_admin_db_pool, import_record_id):
    audit_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_audit_log (action, entity_type, entity_id, import_record_id, actor) "
        "VALUES ('IMPORT', 'catalog_import_record', $1, $1, 'tester') RETURNING id",
        import_record_id,
    )
    assert audit_id is not None


async def test_update_existing_audit_row_denied(catalog_admin_db_pool, import_record_id):
    audit_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_audit_log (action, entity_type, entity_id, actor) "
        "VALUES ('PUBLISH', 'product_formulation', $1, 'tester') RETURNING id",
        import_record_id,
    )
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(
            "UPDATE catalog_audit_log SET reason = 'rewritten after the fact' WHERE id = $1", audit_id,
        )


async def test_delete_audit_row_denied(catalog_admin_db_pool, import_record_id):
    audit_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_audit_log (action, entity_type, entity_id, actor) "
        "VALUES ('PUBLISH', 'product_formulation', $1, 'tester') RETURNING id",
        import_record_id,
    )
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute("DELETE FROM catalog_audit_log WHERE id = $1", audit_id)


# ---------------------------------------------------------------------------
# catalog_formulation_provenance: permanent.
# ---------------------------------------------------------------------------


async def test_insert_provenance_still_works(catalog_admin_db_pool, import_record_id, source_id):
    brand_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO brands (name, normalized_name) VALUES ('Prov Brand', 'prov brand') RETURNING id"
    )
    product_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO products (brand_id, name, normalized_name, category) "
        "VALUES ($1, 'Prov Product', 'prov product', 'moisturizer') RETURNING id",
        brand_id,
    )
    formulation_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO product_formulations (product_id, version, source_type) "
        "VALUES ($1, '1', 'manufacturer_disclosure') RETURNING id",
        product_id,
    )
    provenance_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_formulation_provenance (formulation_id, import_record_id, verification_actor) "
        "VALUES ($1, $2, 'tester') RETURNING id",
        formulation_id, import_record_id,
    )
    assert provenance_id is not None
    return formulation_id


async def _make_provenance_row(catalog_admin_db_pool, import_record_id, suffix):
    brand_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO brands (name, normalized_name) VALUES ($1, $2) RETURNING id",
        f"Prov Brand {suffix}", f"prov brand {suffix}",
    )
    product_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO products (brand_id, name, normalized_name, category) "
        "VALUES ($1, $2, $3, 'moisturizer') RETURNING id",
        brand_id, f"Prov Product {suffix}", f"prov product {suffix}",
    )
    formulation_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO product_formulations (product_id, version, source_type) "
        "VALUES ($1, '1', 'manufacturer_disclosure') RETURNING id",
        product_id,
    )
    provenance_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_formulation_provenance (formulation_id, import_record_id, verification_actor) "
        "VALUES ($1, $2, 'tester') RETURNING id",
        formulation_id, import_record_id,
    )
    return provenance_id


async def test_update_provenance_denied(catalog_admin_db_pool, import_record_id):
    provenance_id = await _make_provenance_row(catalog_admin_db_pool, import_record_id, "upd")
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(
            "UPDATE catalog_formulation_provenance SET verification_actor = 'someone-else' WHERE id = $1",
            provenance_id,
        )


async def test_delete_provenance_denied(catalog_admin_db_pool, import_record_id):
    provenance_id = await _make_provenance_row(catalog_admin_db_pool, import_record_id, "del")
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await catalog_admin_db_pool.execute(
            "DELETE FROM catalog_formulation_provenance WHERE id = $1", provenance_id,
        )
