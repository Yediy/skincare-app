"""app.domain.catalog_publication_service -- real Postgres, through
catalog_admin_db_pool. Covers Section 22's "Publication",
"Reformulation", and part of "Idempotency/concurrency" test lists, plus
the structural safety backstop (Section 16).
"""
import asyncio
import json
import uuid

import pytest

from app.domain.catalog_ingestion_service import CatalogIngestionService
from app.domain.catalog_publication_service import CatalogPublicationService, PublicationError, PublicationOutcome


def _record(external_id, **overrides):
    base = {
        "external_record_id": external_id,
        "brand_name": "Publish Brand",
        "product_name": "Publish Product",
        "category": "moisturizer",
        "market_or_region": "global",
        "formulation_version": "1",
        "source_type": "manufacturer_disclosure",
        "ingredient_list_complete": True,
        "ingredients": [{"raw_name": "Water", "position": 1}],
        "skus": [{"sku": f"PUB-{external_id}"}],
    }
    base.update(overrides)
    return base


@pytest.fixture
async def source_id(catalog_admin_db_pool, clean_catalog_ingestion):
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Water', 'water')"
    )
    return await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_sources (name, normalized_name, source_type) "
        "VALUES ('Publish Source', 'publish source', 'curated_dataset') RETURNING id"
    )


async def _import_and_validate(pool, source_id, record_dict) -> uuid.UUID:
    ingestion = CatalogIngestionService(pool)
    body = json.dumps([record_dict]).encode("utf-8")
    outcome = await ingestion.import_file(source_id=source_id, file_bytes=body, file_format="json")
    await ingestion.validate_batch(outcome.batch_id)
    row = await pool.fetchrow(
        "SELECT id FROM catalog_import_records WHERE batch_id = $1", outcome.batch_id,
    )
    return row["id"]


@pytest.fixture
def publisher(catalog_admin_db_pool):
    return CatalogPublicationService(catalog_admin_db_pool)


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


async def test_publish_creates_full_chain(publisher, source_id, catalog_admin_db_pool):
    import_record_id = await _import_and_validate(catalog_admin_db_pool, source_id, _record("p1"))
    outcome = await publisher.publish(import_record_id, actor="tester")

    formulation = await catalog_admin_db_pool.fetchrow(
        "SELECT * FROM product_formulations WHERE id = $1", outcome.formulation_id,
    )
    assert formulation["publication_status"] == "PUBLISHED"
    assert formulation["is_current"] is True
    assert formulation["ingredient_data_status"] == "COMPLETE"

    ingredient_rows = await catalog_admin_db_pool.fetch(
        "SELECT * FROM formulation_ingredients WHERE formulation_id = $1", outcome.formulation_id,
    )
    assert len(ingredient_rows) == 1

    sku_row = await catalog_admin_db_pool.fetchrow(
        "SELECT * FROM product_skus WHERE formulation_id = $1", outcome.formulation_id,
    )
    assert sku_row["sku"] == "PUB-p1"

    provenance = await catalog_admin_db_pool.fetchrow(
        "SELECT * FROM catalog_formulation_provenance WHERE formulation_id = $1", outcome.formulation_id,
    )
    assert provenance is not None
    assert provenance["import_record_id"] == import_record_id
    assert provenance["verification_actor"] == "tester"
    assert provenance["verified_at"] is not None

    audit_rows = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_audit_log WHERE entity_id = $1 AND action = 'PUBLISH'", outcome.formulation_id,
    )
    assert len(audit_rows) == 1

    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status, formulation_id FROM catalog_import_records WHERE id = $1", import_record_id,
    )
    assert record_row["status"] == "PUBLISHED"
    assert record_row["formulation_id"] == outcome.formulation_id


async def test_publish_sets_partial_when_not_claiming_complete(publisher, source_id, catalog_admin_db_pool):
    import_record_id = await _import_and_validate(
        catalog_admin_db_pool, source_id, _record("p2", ingredient_list_complete=False),
    )
    outcome = await publisher.publish(import_record_id, actor="tester")
    assert outcome.ingredient_data_status == "PARTIAL"


async def test_publish_sets_unknown_when_no_ingredients(publisher, source_id, catalog_admin_db_pool):
    import_record_id = await _import_and_validate(
        catalog_admin_db_pool, source_id,
        _record("p3", ingredients=[], ingredient_list_complete=False),
    )
    outcome = await publisher.publish(import_record_id, actor="tester")
    assert outcome.ingredient_data_status == "UNKNOWN"


async def test_publish_fails_when_status_is_not_validated(publisher, source_id, catalog_admin_db_pool):
    ingestion = CatalogIngestionService(catalog_admin_db_pool)
    body = json.dumps([_record("p4")]).encode("utf-8")
    outcome = await ingestion.import_file(source_id=source_id, file_bytes=body, file_format="json")
    import_record_id = outcome.records[0].import_record_id  # NORMALIZED, never validated

    with pytest.raises(PublicationError) as exc_info:
        await publisher.publish(import_record_id, actor="tester")
    assert exc_info.value.code == "NOT_PUBLISHABLE_STATUS"


async def test_publish_dry_run_creates_nothing(publisher, source_id, catalog_admin_db_pool):
    import_record_id = await _import_and_validate(catalog_admin_db_pool, source_id, _record("p5"))
    outcome = await publisher.publish(import_record_id, actor="tester", dry_run=True)
    assert outcome.dry_run is True

    product_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM products")
    assert product_count == 0
    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record_id,
    )
    assert record_row["status"] == "VALIDATED"


async def test_publish_sku_conflict_blocks_publication(publisher, source_id, catalog_admin_db_pool):
    """The conflicting SKU appears only AFTER this record already
    passed validate_batch cleanly -- validate_batch's own SKU_CONFLICT
    check (tests/domain/test_catalog_ingestion_service.py) covers the
    case where the conflict already exists at validate time; this
    covers publish()'s independent re-check for a conflict that arose
    in between (defense in depth, same posture as the structural
    ingredient-resolution re-check)."""
    import_record_id = await _import_and_validate(
        catalog_admin_db_pool, source_id, _record("p6", skus=[{"sku": "CONFLICT-SKU"}]),
    )
    other_brand = await catalog_admin_db_pool.fetchval(
        "INSERT INTO brands (name, normalized_name) VALUES ('Other', 'other') RETURNING id"
    )
    other_product = await catalog_admin_db_pool.fetchval(
        "INSERT INTO products (brand_id, name, normalized_name, category) "
        "VALUES ($1, 'Other Product', 'other product', 'moisturizer') RETURNING id",
        other_brand,
    )
    other_formulation = await catalog_admin_db_pool.fetchval(
        "INSERT INTO product_formulations (product_id, version, source_type) "
        "VALUES ($1, '1', 'manufacturer_disclosure') RETURNING id",
        other_product,
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, 'CONFLICT-SKU')",
        other_product, other_formulation,
    )

    with pytest.raises(PublicationError) as exc_info:
        await publisher.publish(import_record_id, actor="tester")
    assert exc_info.value.code == "SKU_CONFLICT"

    product_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM products WHERE normalized_name = 'publish product'"
    )
    assert product_count == 0


async def test_publish_structural_backstop_blocks_unresolved_ingredient(
    publisher, source_id, catalog_admin_db_pool,
):
    """Simulates staging state going stale between validate and
    publish (e.g. an alias later removed) -- publish must re-verify
    resolution itself, never trust a cached VALIDATED status blindly."""
    import_record_id = await _import_and_validate(catalog_admin_db_pool, source_id, _record("p7"))
    # Corrupt the normalized_payload in place to reference an
    # ingredient that cannot resolve, while status stays VALIDATED --
    # this is the "state changed since validation" scenario.
    row = await catalog_admin_db_pool.fetchrow(
        "SELECT normalized_payload FROM catalog_import_records WHERE id = $1", import_record_id,
    )
    payload = json.loads(row["normalized_payload"]) if isinstance(row["normalized_payload"], str) else row["normalized_payload"]
    payload["ingredients"] = [{"raw_name": "Totally Unknown Thing", "position": 1,
                                "declared_concentration": None, "concentration_unit": None}]
    await catalog_admin_db_pool.execute(
        "UPDATE catalog_import_records SET normalized_payload = $2::jsonb WHERE id = $1",
        import_record_id, json.dumps(payload),
    )

    with pytest.raises(PublicationError) as exc_info:
        await publisher.publish(import_record_id, actor="tester")
    assert exc_info.value.code == "STRUCTURAL_VALIDATION_FAILED"

    # No half-effects survive a failed publish (Section 15) -- the
    # whole attempt is one transaction that rolled back in full, so
    # the record is left exactly as it was (VALIDATED, on now-stale
    # data), not silently re-flagged. An operator re-runs
    # validate_batch to get it properly back to NEEDS_REVIEW with a
    # real review item.
    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record_id,
    )
    assert record_row["status"] == "VALIDATED"
    product_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM products")
    assert product_count == 0


async def test_idempotent_republish_of_already_published_record_is_a_no_op(
    publisher, source_id, catalog_admin_db_pool,
):
    import_record_id = await _import_and_validate(catalog_admin_db_pool, source_id, _record("p8"))
    first = await publisher.publish(import_record_id, actor="tester")
    second = await publisher.publish(import_record_id, actor="tester")

    assert second.formulation_id == first.formulation_id
    assert second.reused_existing_formulation is True
    formulation_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM product_formulations WHERE product_id = $1", first.product_id,
    )
    assert formulation_count == 1


# ---------------------------------------------------------------------------
# Reformulation
# ---------------------------------------------------------------------------


async def test_reformulation_supersedes_old_and_publishes_new(publisher, source_id, catalog_admin_db_pool):
    record_a_id = await _import_and_validate(
        catalog_admin_db_pool, source_id, _record("reform-a", formulation_version="1"),
    )
    outcome_a = await publisher.publish(record_a_id, actor="tester")

    # Different SKU (and formulation_version) is enough for the
    # content to genuinely differ from record A -- not treated as a
    # same-content re-import.
    record_b_id = await _import_and_validate(
        catalog_admin_db_pool, source_id,
        _record("reform-b", formulation_version="2", skus=[{"sku": "PUB-reform-b-v2"}]),
    )
    outcome_b = await publisher.publish(record_b_id, actor="tester")

    assert outcome_b.is_reformulation is True
    assert outcome_b.superseded_formulation_id == outcome_a.formulation_id
    assert outcome_b.formulation_id != outcome_a.formulation_id

    old_row = await catalog_admin_db_pool.fetchrow(
        "SELECT is_current, publication_status FROM product_formulations WHERE id = $1", outcome_a.formulation_id,
    )
    assert old_row["is_current"] is False
    assert old_row["publication_status"] == "SUPERSEDED"

    new_row = await catalog_admin_db_pool.fetchrow(
        "SELECT is_current, publication_status FROM product_formulations WHERE id = $1", outcome_b.formulation_id,
    )
    assert new_row["is_current"] is True
    assert new_row["publication_status"] == "PUBLISHED"

    # Old formulation is never deleted.
    still_exists = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM product_formulations WHERE id = $1", outcome_a.formulation_id,
    )
    assert still_exists == 1

    current_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM product_formulations WHERE product_id = $1 AND market_or_region = 'global' AND is_current = true",
        outcome_a.product_id,
    )
    assert current_count == 1

    supersede_audit = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_audit_log WHERE entity_id = $1 AND action = 'SUPERSEDE'", outcome_a.formulation_id,
    )
    assert len(supersede_audit) == 1


async def test_reimport_identical_content_reuses_published_formulation_not_a_reformulation(
    publisher, source_id, catalog_admin_db_pool,
):
    """Section 9 extended to publication: the SAME record content,
    re-imported through a brand-new batch/import-record row, must
    reuse the existing published formulation rather than creating a
    pointless new "reformulation"."""
    ingestion = CatalogIngestionService(catalog_admin_db_pool)

    record = _record("reimport-1")
    body_v1 = json.dumps([record, _record("filler-a")]).encode("utf-8")
    outcome1 = await ingestion.import_file(source_id=source_id, file_bytes=body_v1, file_format="json")
    await ingestion.validate_batch(outcome1.batch_id)
    import_record_1 = await catalog_admin_db_pool.fetchrow(
        "SELECT id FROM catalog_import_records WHERE batch_id = $1 AND external_record_id = 'reimport-1'",
        outcome1.batch_id,
    )
    published = await publisher.publish(import_record_1["id"], actor="tester")

    # A different overall file (different filler record) but the SAME
    # exact bytes for "reimport-1" itself -- different batch, brand
    # new import_record row for "reimport-1", identical payload_sha256.
    body_v2 = json.dumps([record, _record("filler-b")]).encode("utf-8")
    outcome2 = await ingestion.import_file(source_id=source_id, file_bytes=body_v2, file_format="json")
    assert outcome2.batch_id != outcome1.batch_id
    await ingestion.validate_batch(outcome2.batch_id)
    import_record_2 = await catalog_admin_db_pool.fetchrow(
        "SELECT id FROM catalog_import_records WHERE batch_id = $1 AND external_record_id = 'reimport-1'",
        outcome2.batch_id,
    )
    assert import_record_2["id"] != import_record_1["id"]

    republished = await publisher.publish(import_record_2["id"], actor="tester")

    assert republished.formulation_id == published.formulation_id
    assert republished.reused_existing_formulation is True
    formulation_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM product_formulations WHERE product_id = $1", published.product_id,
    )
    assert formulation_count == 1


async def test_formulation_version_conflict_when_same_version_different_content(
    publisher, source_id, catalog_admin_db_pool,
):
    record_a_id = await _import_and_validate(
        catalog_admin_db_pool, source_id, _record("conflict-a", formulation_version="1"),
    )
    await publisher.publish(record_a_id, actor="tester")

    record_b_id = await _import_and_validate(
        catalog_admin_db_pool, source_id,
        _record("conflict-b", formulation_version="1", skus=[{"sku": "DIFFERENT-SKU"}]),
    )
    with pytest.raises(PublicationError) as exc_info:
        await publisher.publish(record_b_id, actor="tester")
    assert exc_info.value.code == "FORMULATION_VERSION_CONFLICT"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_two_concurrent_publications_same_product_market_preserve_unique_current(
    source_id, catalog_admin_db_pool,
):
    record_a_id = await _import_and_validate(
        catalog_admin_db_pool, source_id, _record("race-a", formulation_version="1"),
    )
    await CatalogPublicationService(catalog_admin_db_pool).publish(record_a_id, actor="tester")

    record_b_id = await _import_and_validate(
        catalog_admin_db_pool, source_id,
        _record("race-b", formulation_version="2", skus=[{"sku": "RACE-B"}]),
    )
    record_c_id = await _import_and_validate(
        catalog_admin_db_pool, source_id,
        _record("race-c", formulation_version="3", skus=[{"sku": "RACE-C"}]),
    )

    async def _publish(record_id):
        try:
            return await CatalogPublicationService(catalog_admin_db_pool).publish(record_id, actor="tester")
        except PublicationError as e:
            return e

    results = await asyncio.gather(_publish(record_b_id), _publish(record_c_id))

    current_rows = await catalog_admin_db_pool.fetch(
        "SELECT id FROM product_formulations WHERE is_current = true"
    )
    # Whatever the outcome (both may succeed sequentially since FOR
    # UPDATE serializes them, or one may see a version conflict if it
    # reran after the other), the database-level invariant must hold:
    # never more than one is_current row for this product/market.
    assert len(current_rows) == 1


async def test_ten_concurrent_publications_of_the_same_record_are_idempotent(
    publisher, source_id, catalog_admin_db_pool,
):
    """Blocker 4 (independent review): the SAME import_record_id
    published by many concurrent callers must resolve to exactly one
    logical publication -- never a duplicate formulation/provenance/
    audit event, and never a low-level uniqueness exception leaking
    out as normal concurrency behavior. `get_import_record_for_update`'s
    `FOR UPDATE` lock is what makes this possible: only one caller ever
    actually walks the create-formulation path; every other caller
    blocks until that transaction commits, then takes the idempotent
    already-PUBLISHED branch."""
    import_record_id = await _import_and_validate(catalog_admin_db_pool, source_id, _record("race-same"))

    async def _publish():
        service = CatalogPublicationService(catalog_admin_db_pool)
        return await service.publish(import_record_id, actor="tester")

    outcomes = await asyncio.gather(*[_publish() for _ in range(10)])

    formulation_ids = {o.formulation_id for o in outcomes}
    assert len(formulation_ids) == 1
    formulation_id = formulation_ids.pop()

    formulation_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM product_formulations WHERE id = $1", formulation_id,
    )
    assert formulation_count == 1

    provenance_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM catalog_formulation_provenance WHERE formulation_id = $1", formulation_id,
    )
    assert provenance_count == 1

    publish_audit_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM catalog_audit_log WHERE entity_id = $1 AND action = 'PUBLISH'", formulation_id,
    )
    assert publish_audit_count == 1

    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status, formulation_id FROM catalog_import_records WHERE id = $1", import_record_id,
    )
    assert record_row["status"] == "PUBLISHED"
    assert record_row["formulation_id"] == formulation_id


async def test_two_new_records_racing_to_introduce_the_same_brand_and_product(
    publisher, source_id, catalog_admin_db_pool,
):
    """Blocker 4 (independent review): two DIFFERENT, valid import
    records concurrently introducing the same previously-unseen
    normalized brand/product must converge on one logical brand and
    one logical product identity -- never a duplicate, never an
    uncaught uniqueness failure. Each record uses its own distinct SKU
    and category so this test isolates the brand/product identity race
    specifically, not the same-product reformulation path."""
    # Different formulation_version (and SKU) on each so this isolates
    # the brand/product IDENTITY race specifically -- distinct from
    # test_two_concurrent_publications_same_product_market_preserve_
    # unique_current's own already-covered version-conflict/
    # reformulation race for the SAME version.
    record_a_id = await _import_and_validate(
        catalog_admin_db_pool, source_id,
        _record(
            "identity-race-a", brand_name="Shared New Brand", product_name="Shared New Product",
            category="moisturizer", formulation_version="1", skus=[{"sku": "IDRACE-A"}],
        ),
    )
    record_b_id = await _import_and_validate(
        catalog_admin_db_pool, source_id,
        _record(
            "identity-race-b", brand_name="Shared New Brand", product_name="Shared New Product",
            category="moisturizer", formulation_version="2", skus=[{"sku": "IDRACE-B"}],
        ),
    )

    async def _publish(record_id):
        service = CatalogPublicationService(catalog_admin_db_pool)
        try:
            return await service.publish(record_id, actor="tester")
        except PublicationError as e:
            return e

    # asyncio.gather (no return_exceptions) re-raises anything that
    # isn't a PublicationError the _publish() helper already caught --
    # a race producing a raw asyncpg UniqueViolationError would fail
    # this test right here, exactly as required ("never an uncaught
    # uniqueness failure").
    results = await asyncio.gather(_publish(record_a_id), _publish(record_b_id))

    brand_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM brands WHERE normalized_name = 'shared new brand'"
    )
    assert brand_count == 1
    product_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM products WHERE normalized_name = 'shared new product'"
    )
    assert product_count == 1

    # Different formulation_version each, so both legitimately succeed
    # (one becomes the reformulation-supersession of the other) --
    # both reference the SAME product_id, same logical identity,
    # regardless of which call happened to create the row.
    successful = [r for r in results if isinstance(r, PublicationOutcome)]
    assert len(successful) == 2
    product_ids = {r.product_id for r in successful}
    assert len(product_ids) == 1
    brand_ids = {r.brand_id for r in successful}
    assert len(brand_ids) == 1
