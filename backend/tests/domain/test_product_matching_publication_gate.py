"""Section 21's required regression suite: every publication_status
other than PUBLISHED (plus PARTIAL/UNKNOWN ingredient_data_status,
already covered by tests/domain/test_product_matching_service.py's
pre-existing incomplete-data tests) must be invisible to
ProductMatchingService's recommendation candidate query -- never
reaching SafetyEngine at all, let alone being recommended. Uses its
own minimal, isolated catalog rows (not synthetic_catalog) so this
file's publication_status permutations can't collide with any other
test's assumptions about that fixture's shared formulations.
"""
import pytest

from app.domain.product_matching_service import ProductMatchingService
from app.domain.safety_engine import SafetyEngine

NO_CONSTRAINTS = {"allergies": [], "avoid_ingredients": []}
CATEGORY = "publication_gate_test_category"


@pytest.fixture
async def gate_brand_id(db_pool, clean_catalog_ingestion):
    brand_id = await db_pool.fetchval(
        "INSERT INTO brands (name, normalized_name) VALUES ('Gate Brand', 'gate brand') RETURNING id"
    )
    await db_pool.execute(
        "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ('Gate Water', 'gate water')"
    )
    return brand_id


async def _make_formulation(db_pool, brand_id, *, publication_status, suffix,
                             ingredient_data_status="COMPLETE", is_current=True):
    product_id = await db_pool.fetchval(
        "INSERT INTO products (brand_id, name, normalized_name, category, status) "
        "VALUES ($1, $2, $3, $4, 'active') RETURNING id",
        brand_id, f"Gate Product {suffix}", f"gate product {suffix}", CATEGORY,
    )
    formulation_id = await db_pool.fetchval(
        """
        INSERT INTO product_formulations
            (product_id, version, source_type, verified_at, ingredient_data_status,
             market_or_region, publication_status, is_current)
        VALUES ($1, '1', 'manufacturer_disclosure', now(), $2, 'global', $3, $4)
        RETURNING id
        """,
        product_id, ingredient_data_status, publication_status, is_current,
    )
    await db_pool.execute(
        "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, $3)",
        product_id, formulation_id, f"GATE-{suffix}",
    )
    ingredient_id = await db_pool.fetchval("SELECT id FROM ingredients WHERE normalized_name = 'gate water'")
    await db_pool.execute(
        "INSERT INTO formulation_ingredients (formulation_id, ingredient_id, position) VALUES ($1, $2, 1)",
        formulation_id, ingredient_id,
    )
    return product_id, formulation_id


@pytest.fixture
def matcher(app_db_pool):
    return ProductMatchingService(app_db_pool, SafetyEngine())


@pytest.mark.parametrize("publication_status", ["DRAFT", "NEEDS_REVIEW", "VERIFIED", "REJECTED", "SUPERSEDED"])
async def test_non_published_formulation_is_never_a_candidate(
    matcher, db_pool, gate_brand_id, publication_status,
):
    # SUPERSEDED/REJECTED formulations are also never is_current in
    # real data, but is_current=True here deliberately isolates
    # publication_status itself as the excluding factor, independent
    # of is_current.
    await _make_formulation(db_pool, gate_brand_id, publication_status=publication_status, suffix=publication_status)
    matches = await matcher.find_compatible_products(CATEGORY, NO_CONSTRAINTS, max_results=10)
    assert matches == []


async def test_published_and_complete_formulation_is_a_candidate(matcher, db_pool, gate_brand_id):
    _, formulation_id = await _make_formulation(db_pool, gate_brand_id, publication_status="PUBLISHED", suffix="ok")
    matches = await matcher.find_compatible_products(CATEGORY, NO_CONSTRAINTS, max_results=10)
    assert {m.formulation_id for m in matches} == {formulation_id}


async def test_published_but_partial_data_is_still_excluded(matcher, db_pool, gate_brand_id):
    """publication_status alone is not sufficient -- ingredient_data_status
    must independently also be COMPLETE (pre-existing gate, re-proven
    here in combination with the new one)."""
    await _make_formulation(
        db_pool, gate_brand_id, publication_status="PUBLISHED", ingredient_data_status="PARTIAL", suffix="partial",
    )
    matches = await matcher.find_compatible_products(CATEGORY, NO_CONSTRAINTS, max_results=10)
    assert matches == []


async def test_published_but_unknown_data_is_still_excluded(matcher, db_pool, gate_brand_id):
    await _make_formulation(
        db_pool, gate_brand_id, publication_status="PUBLISHED", ingredient_data_status="UNKNOWN", suffix="unknown",
    )
    matches = await matcher.find_compatible_products(CATEGORY, NO_CONSTRAINTS, max_results=10)
    assert matches == []


async def test_published_but_not_current_is_excluded(matcher, db_pool, gate_brand_id):
    await _make_formulation(
        db_pool, gate_brand_id, publication_status="PUBLISHED", is_current=False, suffix="notcurrent",
    )
    matches = await matcher.find_compatible_products(CATEGORY, NO_CONSTRAINTS, max_results=10)
    assert matches == []
