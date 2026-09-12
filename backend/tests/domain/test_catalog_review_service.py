"""app.domain.catalog_review_service -- real Postgres, through
catalog_admin_db_pool. Covers Section 22's remaining "Ingredient
resolution" cases (conflicting alias fails closed) and the human
resolution actions Section 6 requires.
"""
import json

import pytest

from app.domain.catalog_ingestion_service import CatalogIngestionService
from app.domain.catalog_review_service import AliasConflictError, CatalogReviewService, ReviewError


def _record(external_id, **overrides):
    base = {
        "external_record_id": external_id,
        "brand_name": "Review Brand",
        "product_name": f"Review Product {external_id}",
        "category": "moisturizer",
        "market_or_region": "global",
        "formulation_version": "1",
        "source_type": "manufacturer_disclosure",
        "ingredient_list_complete": True,
        "ingredients": [{"raw_name": "Mystery Compound", "position": 1}],
        "skus": [{"sku": f"REV-{external_id}"}],
    }
    base.update(overrides)
    return base


@pytest.fixture
async def source_id(catalog_admin_db_pool, clean_catalog_ingestion):
    return await catalog_admin_db_pool.fetchval(
        "INSERT INTO catalog_sources (name, normalized_name, source_type) "
        "VALUES ('Review Source', 'review source', 'curated_dataset') RETURNING id"
    )


async def _needs_review_record(pool, source_id, external_id="r1", **overrides):
    ingestion = CatalogIngestionService(pool)
    body = json.dumps([_record(external_id, **overrides)]).encode("utf-8")
    outcome = await ingestion.import_file(source_id=source_id, file_bytes=body, file_format="json")
    await ingestion.validate_batch(outcome.batch_id)
    import_record = await pool.fetchrow(
        "SELECT * FROM catalog_import_records WHERE batch_id = $1", outcome.batch_id,
    )
    review_item = await pool.fetchrow(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1", import_record["id"],
    )
    return import_record, review_item


@pytest.fixture
def review_service(catalog_admin_db_pool):
    return CatalogReviewService(catalog_admin_db_pool)


async def test_list_open_returns_the_flagged_item(review_service, source_id, catalog_admin_db_pool):
    _, review_item = await _needs_review_record(catalog_admin_db_pool, source_id)
    items = await review_service.list_open(reason_code="UNKNOWN_INGREDIENT")
    assert any(i["id"] == review_item["id"] for i in items)


async def test_show_returns_unresolved_ingredient_context(review_service, source_id, catalog_admin_db_pool):
    _, review_item = await _needs_review_record(catalog_admin_db_pool, source_id)
    detail = await review_service.show(review_item["id"])
    assert detail.context["unresolved_ingredient_names"] == ["Mystery Compound"]


async def test_map_to_existing_ingredient_creates_alias_and_resolves_review(
    review_service, source_id, catalog_admin_db_pool,
):
    ingredient_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Niacinamide', 'niacinamide') RETURNING id"
    )
    import_record, review_item = await _needs_review_record(catalog_admin_db_pool, source_id)

    result = await review_service.map_ingredient(
        review_item["id"], raw_name="Mystery Compound", ingredient_id=ingredient_id, actor="reviewer1",
    )
    assert result["status"] == "RESOLVED"
    assert result["resolution"] == "MAPPED_TO_EXISTING_INGREDIENT"
    assert result["reviewed_by"] == "reviewer1"

    alias = await catalog_admin_db_pool.fetchrow(
        "SELECT * FROM ingredient_aliases WHERE normalized_alias = 'mystery compound'"
    )
    assert alias is not None
    assert alias["ingredient_id"] == ingredient_id

    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "VALIDATED"

    audit = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_audit_log WHERE action = 'ADD_ALIAS' AND entity_id = $1", ingredient_id,
    )
    assert len(audit) == 1


async def test_map_ingredient_fails_closed_on_conflicting_alias(review_service, source_id, catalog_admin_db_pool):
    """Section 17: the raw string already means a DIFFERENT ingredient
    -- must fail closed, never silently overwrite. Uses a genuinely
    unresolved ingredient ("Mystery Compound") to produce the review
    item -- "Water" here is deliberately just the raw_name a reviewer
    is (mistakenly) attempting to map, independent of what actually
    triggered this review item, to isolate the alias-conflict check
    itself."""
    existing_ingredient = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Water', 'water') RETURNING id"
    )
    other_ingredient = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Glycerin', 'glycerin') RETURNING id"
    )
    _, review_item = await _needs_review_record(catalog_admin_db_pool, source_id)
    # "Water" is already a canonical ingredient name -- mapping it to a
    # DIFFERENT ingredient must fail closed.
    with pytest.raises(AliasConflictError):
        await review_service.map_ingredient(
            review_item["id"], raw_name="Water", ingredient_id=other_ingredient, actor="reviewer1",
        )

    unchanged = await catalog_admin_db_pool.fetchrow(
        "SELECT normalized_name FROM ingredients WHERE id = $1", existing_ingredient,
    )
    assert unchanged["normalized_name"] == "water"
    review_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_review_items WHERE id = $1", review_item["id"],
    )
    assert review_row["status"] == "OPEN"


async def test_map_ingredient_conflicting_with_existing_alias_fails_closed(
    review_service, source_id, catalog_admin_db_pool,
):
    real_ingredient = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Retinol', 'retinol') RETURNING id"
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredient_aliases (ingredient_id, alias, normalized_alias) VALUES ($1, 'Vitamin A1', 'vitamin a1')",
        real_ingredient,
    )
    wrong_ingredient = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Niacinamide', 'niacinamide') RETURNING id"
    )
    _, review_item = await _needs_review_record(catalog_admin_db_pool, source_id)
    with pytest.raises(AliasConflictError) as exc_info:
        await review_service.map_ingredient(
            review_item["id"], raw_name="Vitamin A1", ingredient_id=wrong_ingredient, actor="reviewer1",
        )
    assert exc_info.value.conflict["conflict_type"] == "IS_EXISTING_ALIAS"


async def test_map_ingredient_idempotent_when_alias_already_points_at_same_ingredient(
    review_service, source_id, catalog_admin_db_pool,
):
    ingredient_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Retinol', 'retinol') RETURNING id"
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredient_aliases (ingredient_id, alias, normalized_alias) VALUES ($1, 'Vitamin A1', 'vitamin a1')",
        ingredient_id,
    )
    _, review_item = await _needs_review_record(catalog_admin_db_pool, source_id)
    result = await review_service.map_ingredient(
        review_item["id"], raw_name="Vitamin A1", ingredient_id=ingredient_id, actor="reviewer1",
    )
    assert result["status"] == "RESOLVED"
    alias_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM ingredient_aliases WHERE normalized_alias = 'vitamin a1'"
    )
    assert alias_count == 1


async def test_create_new_ingredient_and_resolve(review_service, source_id, catalog_admin_db_pool):
    import_record, review_item = await _needs_review_record(
        catalog_admin_db_pool, source_id, ingredients=[{"raw_name": "Brand New Peptide Complex", "position": 1}],
    )
    result = await review_service.create_ingredient(
        review_item["id"], raw_name="Brand New Peptide Complex", canonical_name="Peptide Complex XJ-9",
        ingredient_type="active", actor="reviewer1",
    )
    assert result["resolution"] == "CREATED_NEW_INGREDIENT"

    ingredient = await catalog_admin_db_pool.fetchrow(
        "SELECT * FROM ingredients WHERE normalized_name = 'peptide complex xj-9'"
    )
    assert ingredient is not None
    alias = await catalog_admin_db_pool.fetchrow(
        "SELECT * FROM ingredient_aliases WHERE normalized_alias = 'brand new peptide complex'"
    )
    assert alias is not None
    assert alias["ingredient_id"] == ingredient["id"]

    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "VALIDATED"


async def test_create_new_ingredient_no_alias_needed_when_raw_name_matches_canonical(
    review_service, source_id, catalog_admin_db_pool,
):
    _, review_item = await _needs_review_record(
        catalog_admin_db_pool, source_id, ingredients=[{"raw_name": "Squalane", "position": 1}],
    )
    await review_service.create_ingredient(
        review_item["id"], raw_name="Squalane", canonical_name="Squalane", actor="reviewer1",
    )
    alias_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM ingredient_aliases")
    assert alias_count == 0


async def test_create_ingredient_rejects_when_canonical_already_resolves(
    review_service, source_id, catalog_admin_db_pool,
):
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Water', 'water')"
    )
    _, review_item = await _needs_review_record(catalog_admin_db_pool, source_id)
    with pytest.raises(ReviewError) as exc_info:
        await review_service.create_ingredient(
            review_item["id"], raw_name="Mystery Compound", canonical_name="Water", actor="reviewer1",
        )
    assert exc_info.value.code == "INGREDIENT_ALREADY_EXISTS"


async def test_reject_import_record_marks_record_rejected(review_service, source_id, catalog_admin_db_pool):
    import_record, review_item = await _needs_review_record(catalog_admin_db_pool, source_id)
    result = await review_service.reject_import_record(
        review_item["id"], reason="Source data is unreliable for this SKU", actor="reviewer1",
    )
    assert result["resolution"] == "REJECTED_SOURCE_VALUE"

    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "REJECTED"

    audit = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_audit_log WHERE action = 'REJECT' AND entity_id = $1", import_record["id"],
    )
    assert len(audit) == 1


async def test_dismiss_confirms_identity_and_revalidates(review_service, source_id, catalog_admin_db_pool):
    other_brand = await catalog_admin_db_pool.fetchval(
        "INSERT INTO brands (name, normalized_name) VALUES ('Existing', 'existing') RETURNING id"
    )
    other_product = await catalog_admin_db_pool.fetchval(
        "INSERT INTO products (brand_id, name, normalized_name, category) "
        "VALUES ($1, 'Existing Product', 'existing product', 'moisturizer') RETURNING id",
        other_brand,
    )
    other_formulation = await catalog_admin_db_pool.fetchval(
        "INSERT INTO product_formulations (product_id, version, source_type) "
        "VALUES ($1, '1', 'manufacturer_disclosure') RETURNING id",
        other_product,
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, 'DISMISS-SKU')",
        other_product, other_formulation,
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Mystery Compound', 'mystery compound')"
    )
    import_record, _ = await _needs_review_record(
        catalog_admin_db_pool, source_id, skus=[{"sku": "DISMISS-SKU"}],
    )
    review_item = await catalog_admin_db_pool.fetchrow(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1 AND reason_code = 'SKU_CONFLICT'",
        import_record["id"],
    )
    result = await review_service.dismiss(
        review_item["id"], resolution="IDENTITY_CONFIRMED", actor="reviewer1", notes="Verified same product line",
    )
    assert result["status"] == "RESOLVED"

    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "VALIDATED"


async def test_resolving_one_of_two_open_items_does_not_revalidate_yet(
    review_service, source_id, catalog_admin_db_pool,
):
    """A record with TWO independent problems only becomes VALIDATED
    again once BOTH are resolved."""
    other_brand = await catalog_admin_db_pool.fetchval(
        "INSERT INTO brands (name, normalized_name) VALUES ('Existing2', 'existing2') RETURNING id"
    )
    other_product = await catalog_admin_db_pool.fetchval(
        "INSERT INTO products (brand_id, name, normalized_name, category) "
        "VALUES ($1, 'Existing2 Product', 'existing2 product', 'moisturizer') RETURNING id",
        other_brand,
    )
    other_formulation = await catalog_admin_db_pool.fetchval(
        "INSERT INTO product_formulations (product_id, version, source_type) "
        "VALUES ($1, '1', 'manufacturer_disclosure') RETURNING id",
        other_product,
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, 'DOUBLE-SKU')",
        other_product, other_formulation,
    )
    import_record, _ = await _needs_review_record(
        catalog_admin_db_pool, source_id, skus=[{"sku": "DOUBLE-SKU"}],
    )
    review_items = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1", import_record["id"],
    )
    assert len(review_items) == 2  # UNKNOWN_INGREDIENT + SKU_CONFLICT

    sku_item = next(r for r in review_items if r["reason_code"] == "SKU_CONFLICT")
    await review_service.dismiss(sku_item["id"], resolution="IDENTITY_CONFIRMED", actor="reviewer1")

    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "NEEDS_REVIEW"


async def test_resolving_already_resolved_item_fails(review_service, source_id, catalog_admin_db_pool):
    _, review_item = await _needs_review_record(catalog_admin_db_pool, source_id)
    await review_service.reject_import_record(review_item["id"], reason="x", actor="reviewer1")
    with pytest.raises(ReviewError):
        await review_service.reject_import_record(review_item["id"], reason="y", actor="reviewer1")
