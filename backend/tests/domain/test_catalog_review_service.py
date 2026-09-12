"""app.domain.catalog_review_service -- real Postgres, through
catalog_admin_db_pool. Covers Section 22's remaining "Ingredient
resolution" cases (conflicting alias fails closed) and the human
resolution actions Section 6 requires.
"""
import json

import pytest

from app.domain.catalog_ingestion_service import CatalogIngestionService
from app.domain.catalog_review_service import CatalogReviewService, ReviewError


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
        review_item["id"], ingredient_id=ingredient_id, actor="reviewer1",
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


async def test_map_ingredient_fails_closed_when_target_resolved_out_of_band_via_canonical(
    review_service, source_id, catalog_admin_db_pool,
):
    """Identity binding (hotfix): `_resolve_target_raw_name` requires
    the review item's own target raw string to still be genuinely
    unresolved. If, between the review item being opened and a
    resolution attempt, some other process makes that exact raw
    string resolve on its own (e.g. a canonical ingredient of the
    same name gets added out-of-band -- simulated here by inserting
    it directly, standing in for a genuine two-connection race),
    acting on stale state must be refused rather than silently
    treated as a no-op or silently re-pointed at whatever ingredient
    this caller happened to pass."""
    other_ingredient = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Glycerin', 'glycerin') RETURNING id"
    )
    _, review_item = await _needs_review_record(
        catalog_admin_db_pool, source_id, ingredients=[{"raw_name": "Water", "position": 1}],
    )
    # Out-of-band: "Water" becomes a real canonical ingredient AFTER
    # the review item was already opened for it.
    existing_ingredient = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Water', 'water') RETURNING id"
    )
    with pytest.raises(ReviewError) as exc_info:
        await review_service.map_ingredient(
            review_item["id"], ingredient_id=other_ingredient, actor="reviewer1",
        )
    assert exc_info.value.code == "ALREADY_RESOLVED"

    unchanged = await catalog_admin_db_pool.fetchrow(
        "SELECT normalized_name FROM ingredients WHERE id = $1", existing_ingredient,
    )
    assert unchanged["normalized_name"] == "water"
    alias_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM ingredient_aliases")
    assert alias_count == 0
    review_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_review_items WHERE id = $1", review_item["id"],
    )
    assert review_row["status"] == "OPEN"
    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", review_item["import_record_id"],
    )
    assert record_row["status"] == "NEEDS_REVIEW"


async def test_map_ingredient_fails_closed_when_target_resolved_out_of_band_via_alias(
    review_service, source_id, catalog_admin_db_pool,
):
    real_ingredient = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Retinol', 'retinol') RETURNING id"
    )
    wrong_ingredient = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Niacinamide', 'niacinamide') RETURNING id"
    )
    _, review_item = await _needs_review_record(
        catalog_admin_db_pool, source_id, ingredients=[{"raw_name": "Vitamin A1", "position": 1}],
    )
    # Out-of-band: "Vitamin A1" becomes a resolvable alias AFTER the
    # review item was already opened for it.
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredient_aliases (ingredient_id, alias, normalized_alias) VALUES ($1, 'Vitamin A1', 'vitamin a1')",
        real_ingredient,
    )
    with pytest.raises(ReviewError) as exc_info:
        await review_service.map_ingredient(
            review_item["id"], ingredient_id=wrong_ingredient, actor="reviewer1",
        )
    assert exc_info.value.code == "ALREADY_RESOLVED"
    review_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_review_items WHERE id = $1", review_item["id"],
    )
    assert review_row["status"] == "OPEN"


async def test_map_ingredient_fails_closed_even_when_out_of_band_alias_already_matches_requested_target(
    review_service, source_id, catalog_admin_db_pool,
):
    """Even the "harmless-looking" case -- the out-of-band alias
    already points at the very ingredient this caller is about to
    request -- must still fail closed rather than silently succeed as
    a no-op. Acting on stale state is refused unconditionally; the
    reviewer must re-check the item (it's effectively already
    resolved) rather than this method quietly agreeing with
    whatever happened out from under it."""
    ingredient_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Retinol', 'retinol') RETURNING id"
    )
    _, review_item = await _needs_review_record(
        catalog_admin_db_pool, source_id, ingredients=[{"raw_name": "Vitamin A1", "position": 1}],
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO ingredient_aliases (ingredient_id, alias, normalized_alias) VALUES ($1, 'Vitamin A1', 'vitamin a1')",
        ingredient_id,
    )
    with pytest.raises(ReviewError) as exc_info:
        await review_service.map_ingredient(
            review_item["id"], ingredient_id=ingredient_id, actor="reviewer1",
        )
    assert exc_info.value.code == "ALREADY_RESOLVED"
    alias_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM ingredient_aliases WHERE normalized_alias = 'vitamin a1'"
    )
    assert alias_count == 1
    review_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_review_items WHERE id = $1", review_item["id"],
    )
    assert review_row["status"] == "OPEN"


async def test_create_new_ingredient_and_resolve(review_service, source_id, catalog_admin_db_pool):
    import_record, review_item = await _needs_review_record(
        catalog_admin_db_pool, source_id, ingredients=[{"raw_name": "Brand New Peptide Complex", "position": 1}],
    )
    result = await review_service.create_ingredient(
        review_item["id"], canonical_name="Peptide Complex XJ-9",
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
        review_item["id"], canonical_name="Squalane", actor="reviewer1",
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
            review_item["id"], canonical_name="Water", actor="reviewer1",
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


async def test_two_unknown_ingredients_each_get_their_own_review_item(
    review_service, source_id, catalog_admin_db_pool,
):
    """Blocker 3 (independent review): a record with TWO independent
    unresolved ingredients must not collapse into a single aggregate
    UNKNOWN_INGREDIENT review item -- resolving one must never look
    like "the" ingredient problem is solved."""
    import_record, _ = await _needs_review_record(
        catalog_admin_db_pool, source_id,
        ingredients=[{"raw_name": "Unknown A", "position": 1}, {"raw_name": "Unknown B", "position": 2}],
    )
    review_items = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1 AND reason_code = 'UNKNOWN_INGREDIENT'",
        import_record["id"],
    )
    assert len(review_items) == 2
    identity_keys = {r["identity_key"] for r in review_items}
    assert identity_keys == {"unknown a", "unknown b"}
    assert all(r["status"] == "OPEN" for r in review_items)


async def test_resolving_one_of_two_unknown_ingredients_leaves_record_needs_review(
    review_service, source_id, catalog_admin_db_pool,
):
    ingredient_a = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Canonical A', 'canonical a') RETURNING id"
    )
    import_record, _ = await _needs_review_record(
        catalog_admin_db_pool, source_id,
        ingredients=[{"raw_name": "Unknown A", "position": 1}, {"raw_name": "Unknown B", "position": 2}],
    )
    review_items = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1 AND reason_code = 'UNKNOWN_INGREDIENT'",
        import_record["id"],
    )
    item_a = next(r for r in review_items if r["identity_key"] == "unknown a")
    item_b = next(r for r in review_items if r["identity_key"] == "unknown b")

    await review_service.map_ingredient(
        item_a["id"], ingredient_id=ingredient_a, actor="reviewer1",
    )

    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "NEEDS_REVIEW"

    # Unknown B's own item is untouched and still surfaced for review.
    item_b_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_review_items WHERE id = $1", item_b["id"],
    )
    assert item_b_row["status"] == "OPEN"
    open_items = await review_service.list_open()
    assert any(i["id"] == item_b["id"] for i in open_items)

    # And the record genuinely cannot publish yet.
    from app.domain.catalog_publication_service import CatalogPublicationService, PublicationError
    with pytest.raises(PublicationError) as exc_info:
        await CatalogPublicationService(catalog_admin_db_pool).publish(import_record["id"], actor="tester")
    assert exc_info.value.code == "NOT_PUBLISHABLE_STATUS"


async def test_resolving_both_unknown_ingredients_permits_validated(
    review_service, source_id, catalog_admin_db_pool,
):
    ingredient_a = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Canonical A', 'canonical a') RETURNING id"
    )
    ingredient_b = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Canonical B', 'canonical b') RETURNING id"
    )
    import_record, _ = await _needs_review_record(
        catalog_admin_db_pool, source_id,
        ingredients=[{"raw_name": "Unknown A", "position": 1}, {"raw_name": "Unknown B", "position": 2}],
    )
    review_items = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1 AND reason_code = 'UNKNOWN_INGREDIENT'",
        import_record["id"],
    )
    item_a = next(r for r in review_items if r["identity_key"] == "unknown a")
    item_b = next(r for r in review_items if r["identity_key"] == "unknown b")

    await review_service.map_ingredient(item_a["id"], ingredient_id=ingredient_a, actor="r1")
    await review_service.map_ingredient(item_b["id"], ingredient_id=ingredient_b, actor="r1")

    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "VALIDATED"

    # Publication must behave normally once both identities are
    # genuinely (not falsely) resolved.
    from app.domain.catalog_publication_service import CatalogPublicationService
    outcome = await CatalogPublicationService(catalog_admin_db_pool).publish(import_record["id"], actor="tester")
    assert outcome.formulation_id is not None
    published_record = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert published_record["status"] == "PUBLISHED"


async def test_revalidation_never_duplicates_review_items_on_repeated_resolution(
    review_service, source_id, catalog_admin_db_pool,
):
    """Repeated revalidation passes (one per resolution action) must
    never explode into duplicate review items for the same unresolved
    problem."""
    ingredient_a = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Canonical A', 'canonical a') RETURNING id"
    )
    import_record, _ = await _needs_review_record(
        catalog_admin_db_pool, source_id,
        ingredients=[{"raw_name": "Unknown A", "position": 1}, {"raw_name": "Unknown B", "position": 2}],
    )
    review_items = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1 AND reason_code = 'UNKNOWN_INGREDIENT'",
        import_record["id"],
    )
    item_a = next(r for r in review_items if r["identity_key"] == "unknown a")
    await review_service.map_ingredient(item_a["id"], ingredient_id=ingredient_a, actor="r1")

    total_items = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM catalog_review_items WHERE import_record_id = $1", import_record["id"],
    )
    assert total_items == 2  # still exactly A + B, no duplicate created for B on revalidation


async def test_unknown_ingredient_and_sku_conflict_resolve_independently(
    review_service, source_id, catalog_admin_db_pool,
):
    """Resolving one reason code must never accidentally clear a
    different, unrelated open review item."""
    other_brand = await catalog_admin_db_pool.fetchval(
        "INSERT INTO brands (name, normalized_name) VALUES ('IndepBrand', 'indepbrand') RETURNING id"
    )
    other_product = await catalog_admin_db_pool.fetchval(
        "INSERT INTO products (brand_id, name, normalized_name, category) "
        "VALUES ($1, 'IndepProduct', 'indepproduct', 'moisturizer') RETURNING id",
        other_brand,
    )
    other_formulation = await catalog_admin_db_pool.fetchval(
        "INSERT INTO product_formulations (product_id, version, source_type) "
        "VALUES ($1, '1', 'manufacturer_disclosure') RETURNING id",
        other_product,
    )
    await catalog_admin_db_pool.execute(
        "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, 'INDEP-SKU')",
        other_product, other_formulation,
    )
    ingredient_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Indep Ingredient', 'indep ingredient') RETURNING id"
    )
    import_record, _ = await _needs_review_record(
        catalog_admin_db_pool, source_id, skus=[{"sku": "INDEP-SKU"}],
    )
    review_items = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1", import_record["id"],
    )
    assert {r["reason_code"] for r in review_items} == {"UNKNOWN_INGREDIENT", "SKU_CONFLICT"}
    unknown_item = next(r for r in review_items if r["reason_code"] == "UNKNOWN_INGREDIENT")

    # Resolve only the ingredient -- the SKU conflict must remain open.
    await review_service.map_ingredient(
        unknown_item["id"], ingredient_id=ingredient_id, actor="r1",
    )
    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "NEEDS_REVIEW"
    sku_item_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_review_items WHERE import_record_id = $1 AND reason_code = 'SKU_CONFLICT'",
        import_record["id"],
    )
    assert sku_item_row["status"] == "OPEN"

    # Now resolve the SKU conflict too -- the ingredient resolution
    # from before must not have been undone/duplicated.
    sku_item = await catalog_admin_db_pool.fetchrow(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1 AND reason_code = 'SKU_CONFLICT'",
        import_record["id"],
    )
    await review_service.dismiss(sku_item["id"], resolution="IDENTITY_CONFIRMED", actor="r1")
    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "VALIDATED"
    unknown_item_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM catalog_review_items WHERE import_record_id = $1 AND reason_code = 'UNKNOWN_INGREDIENT'",
        import_record["id"],
    )
    assert unknown_item_count == 1


async def test_resolving_already_resolved_item_fails(review_service, source_id, catalog_admin_db_pool):
    _, review_item = await _needs_review_record(catalog_admin_db_pool, source_id)
    await review_service.reject_import_record(review_item["id"], reason="x", actor="reviewer1")
    with pytest.raises(ReviewError):
        await review_service.reject_import_record(review_item["id"], reason="y", actor="reviewer1")


# ---------------------------------------------------------------------------
# Identity binding hotfix (independent review): map_ingredient()/
# create_ingredient() must never trust a caller-supplied raw_name --
# the raw string is always derived from the review item's OWN
# identity_key, cross-referenced against the import record's current
# normalized_payload (see CatalogReviewService._resolve_target_raw_name).
# Before this fix, an operator (or a scripting mistake) could resolve
# item A's row while creating an alias for a completely unrelated
# string B, leaving A's real problem silently marked RESOLVED without
# ever actually being fixed.
# ---------------------------------------------------------------------------


async def test_map_ingredient_rejects_a_caller_supplied_raw_name_outright(
    review_service, source_id, catalog_admin_db_pool,
):
    """The exploit this hotfix closes required raw_name to be an
    accepted, trusted argument in the first place. It no longer is --
    proven here directly: passing one at all is a hard TypeError, not
    a value that merely gets ignored or silently validated away."""
    ingredient_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Canonical A', 'canonical a') RETURNING id"
    )
    import_record, _ = await _needs_review_record(
        catalog_admin_db_pool, source_id,
        ingredients=[{"raw_name": "Unknown A", "position": 1}, {"raw_name": "Unknown B", "position": 2}],
    )
    review_items = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1 AND reason_code = 'UNKNOWN_INGREDIENT'",
        import_record["id"],
    )
    item_a = next(r for r in review_items if r["identity_key"] == "unknown a")
    item_b = next(r for r in review_items if r["identity_key"] == "unknown b")

    # The old exploit: resolve item A's row while actually supplying
    # item B's raw string. Now impossible to even attempt -- raw_name
    # isn't a parameter of map_ingredient() at all.
    with pytest.raises(TypeError):
        await review_service.map_ingredient(
            item_a["id"], raw_name="Unknown B", ingredient_id=ingredient_id, actor="attacker",
        )

    for item in (item_a, item_b):
        row = await catalog_admin_db_pool.fetchrow(
            "SELECT status FROM catalog_review_items WHERE id = $1", item["id"],
        )
        assert row["status"] == "OPEN"
    alias_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM ingredient_aliases")
    assert alias_count == 0
    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "NEEDS_REVIEW"


async def test_create_ingredient_rejects_a_caller_supplied_raw_name_outright(
    review_service, source_id, catalog_admin_db_pool,
):
    """Same exploit, same fix, via create_ingredient()."""
    import_record, _ = await _needs_review_record(
        catalog_admin_db_pool, source_id,
        ingredients=[{"raw_name": "Unknown A", "position": 1}, {"raw_name": "Unknown B", "position": 2}],
    )
    review_items = await catalog_admin_db_pool.fetch(
        "SELECT * FROM catalog_review_items WHERE import_record_id = $1 AND reason_code = 'UNKNOWN_INGREDIENT'",
        import_record["id"],
    )
    item_a = next(r for r in review_items if r["identity_key"] == "unknown a")
    item_b = next(r for r in review_items if r["identity_key"] == "unknown b")

    with pytest.raises(TypeError):
        await review_service.create_ingredient(
            item_a["id"], raw_name="Unknown B", canonical_name="Some New Canonical", actor="attacker",
        )

    for item in (item_a, item_b):
        row = await catalog_admin_db_pool.fetchrow(
            "SELECT status FROM catalog_review_items WHERE id = $1", item["id"],
        )
        assert row["status"] == "OPEN"
    ingredient_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM ingredients WHERE normalized_name = 'some new canonical'"
    )
    assert ingredient_count == 0
    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "NEEDS_REVIEW"


async def test_map_ingredient_rejects_identity_key_that_no_longer_matches_the_payload(
    review_service, source_id, catalog_admin_db_pool,
):
    """Defense in depth: even if a review item's identity_key were to
    end up not matching anything in its import record's current
    normalized_payload (e.g. data tampering, or a future bug), that
    must be refused with a specific classified error rather than
    silently doing nothing useful or picking an arbitrary ingredient.
    Simulated directly since normalize_record()'s own duplicate-raw-
    name validator makes this state unreachable through the ordinary
    ingestion/validation path."""
    ingredient_id = await catalog_admin_db_pool.fetchval(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Canonical A', 'canonical a') RETURNING id"
    )
    _, review_item = await _needs_review_record(
        catalog_admin_db_pool, source_id, ingredients=[{"raw_name": "Unknown A", "position": 1}],
    )
    await catalog_admin_db_pool.execute(
        "UPDATE catalog_review_items SET identity_key = 'totally unrelated string' WHERE id = $1",
        review_item["id"],
    )
    with pytest.raises(ReviewError) as exc_info:
        await review_service.map_ingredient(review_item["id"], ingredient_id=ingredient_id, actor="reviewer1")
    assert exc_info.value.code == "IDENTITY_KEY_NOT_FOUND"

    row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_review_items WHERE id = $1", review_item["id"],
    )
    assert row["status"] == "OPEN"
    alias_count = await catalog_admin_db_pool.fetchval("SELECT count(*) FROM ingredient_aliases")
    assert alias_count == 0


async def test_create_ingredient_rejects_identity_key_that_no_longer_matches_the_payload(
    review_service, source_id, catalog_admin_db_pool,
):
    _, review_item = await _needs_review_record(
        catalog_admin_db_pool, source_id, ingredients=[{"raw_name": "Unknown A", "position": 1}],
    )
    await catalog_admin_db_pool.execute(
        "UPDATE catalog_review_items SET identity_key = 'totally unrelated string' WHERE id = $1",
        review_item["id"],
    )
    with pytest.raises(ReviewError) as exc_info:
        await review_service.create_ingredient(
            review_item["id"], canonical_name="Some New Canonical", actor="reviewer1",
        )
    assert exc_info.value.code == "IDENTITY_KEY_NOT_FOUND"

    row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_review_items WHERE id = $1", review_item["id"],
    )
    assert row["status"] == "OPEN"
    ingredient_count = await catalog_admin_db_pool.fetchval(
        "SELECT count(*) FROM ingredients WHERE normalized_name = 'some new canonical'"
    )
    assert ingredient_count == 0


async def test_dismiss_rejects_unknown_ingredient_review_items_generically(
    review_service, source_id, catalog_admin_db_pool,
):
    """Generic dismiss() must never be a backdoor around identity
    binding -- an UNKNOWN_INGREDIENT item can only be closed via
    map_ingredient(), create_ingredient(), or reject_import_record().
    In particular, MANUAL_OVERRIDE must never be able to silently
    convert an unresolved ingredient identity into VALIDATED state
    with no ingredient/alias ever actually created."""
    import_record, review_item = await _needs_review_record(catalog_admin_db_pool, source_id)
    assert review_item["reason_code"] == "UNKNOWN_INGREDIENT"

    with pytest.raises(ReviewError) as exc_info:
        await review_service.dismiss(
            review_item["id"], resolution="MANUAL_OVERRIDE", actor="reviewer1", notes="skip it",
        )
    assert exc_info.value.code == "INGREDIENT_REVIEW_REQUIRES_EXPLICIT_RESOLUTION"

    row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_review_items WHERE id = $1", review_item["id"],
    )
    assert row["status"] == "OPEN"
    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", import_record["id"],
    )
    assert record_row["status"] == "NEEDS_REVIEW"
