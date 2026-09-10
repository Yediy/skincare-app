"""app.db.catalog_repository, tested directly through app_db_pool (the
restricted skincare_app role, same as production) against the
synthetic_catalog fixture. Proves the three required deterministic
lookup paths (by product ID, by SKU, by formulation ID) and that
ingredient alias resolution is exact-match and deterministic, not
substring/LIKE matching.
"""
import uuid

from app.db.catalog_repository import (
    canonical_ingredient_pair,
    get_active_rules_for_ingredients,
    get_current_formulation_for_product,
    get_formulation_by_id,
    get_formulation_by_sku,
    get_formulation_ingredients,
    get_interactions_within,
    get_product_by_id,
    normalize_name,
    resolve_ingredient,
)


async def test_get_formulation_by_id(app_db_pool, synthetic_catalog):
    formulation = await get_formulation_by_id(app_db_pool, synthetic_catalog["formulations"]["retinol"])
    assert formulation is not None
    assert formulation["product_name"] == "Renewal Night Retinol Serum"
    assert formulation["is_current"] is True


async def test_get_formulation_by_id_returns_none_for_unknown_id(app_db_pool):
    assert await get_formulation_by_id(app_db_pool, uuid.uuid4()) is None


async def test_get_current_formulation_for_product(app_db_pool, synthetic_catalog):
    formulation = await get_current_formulation_for_product(app_db_pool, synthetic_catalog["products"]["retinol"])
    assert formulation is not None
    assert formulation["id"] == synthetic_catalog["formulations"]["retinol"]


async def test_get_formulation_by_sku(app_db_pool, synthetic_catalog):
    formulation = await get_formulation_by_sku(app_db_pool, "TNX-RETIN-01")
    assert formulation is not None
    assert formulation["id"] == synthetic_catalog["formulations"]["retinol"]


async def test_get_formulation_by_sku_scoped_to_product_id(app_db_pool, synthetic_catalog):
    formulation = await get_formulation_by_sku(
        app_db_pool, "TNX-RETIN-01", product_id=synthetic_catalog["products"]["retinol"]
    )
    assert formulation is not None
    assert formulation["id"] == synthetic_catalog["formulations"]["retinol"]


async def test_get_formulation_by_sku_returns_none_for_unknown_sku(app_db_pool, synthetic_catalog):
    assert await get_formulation_by_sku(app_db_pool, "NOT-A-REAL-SKU") is None


async def test_get_product_by_id(app_db_pool, synthetic_catalog):
    product = await get_product_by_id(app_db_pool, synthetic_catalog["products"]["retinol"])
    assert product["name"] == "Renewal Night Retinol Serum"
    assert product["brand_name"] == "Testonyx"


def test_normalize_name_collapses_whitespace_and_case():
    assert normalize_name("  Vitamin   C  ") == "vitamin c"
    assert normalize_name("ASCORBIC ACID") == "ascorbic acid"


async def test_resolve_ingredient_by_canonical_name(app_db_pool, synthetic_catalog):
    ingredient = await resolve_ingredient(app_db_pool, "Retinol")
    assert ingredient["id"] == synthetic_catalog["ingredients"]["retinol"]


async def test_resolve_ingredient_by_canonical_name_case_insensitive_and_whitespace(app_db_pool, synthetic_catalog):
    ingredient = await resolve_ingredient(app_db_pool, "  ReTinOl  ")
    assert ingredient["id"] == synthetic_catalog["ingredients"]["retinol"]


async def test_resolve_ingredient_by_alias(app_db_pool, synthetic_catalog):
    ingredient = await resolve_ingredient(app_db_pool, "Vitamin A1")
    assert ingredient["id"] == synthetic_catalog["ingredients"]["retinol"]


async def test_resolve_ingredient_by_alias_case_insensitive(app_db_pool, synthetic_catalog):
    ingredient = await resolve_ingredient(app_db_pool, "vitamin a1")
    assert ingredient["id"] == synthetic_catalog["ingredients"]["retinol"]


async def test_resolve_ingredient_never_substring_matches(app_db_pool, synthetic_catalog):
    """"Retino" is a substring of "Retinol" but not the ingredient (or
    any alias) itself -- must resolve to nothing, not fuzzily match."""
    assert await resolve_ingredient(app_db_pool, "Retino") is None


async def test_resolve_ingredient_returns_none_for_unknown_name(app_db_pool, synthetic_catalog):
    assert await resolve_ingredient(app_db_pool, "Completely Unknown Compound") is None


async def test_get_formulation_ingredients_preserves_declared_order(app_db_pool, synthetic_catalog):
    ingredients = await get_formulation_ingredients(app_db_pool, synthetic_catalog["formulations"]["moisturizer"])
    names = [i["canonical_name"] for i in ingredients]
    assert names == ["Water", "Glycerin", "Dimethicone", "Shea Butter"]


async def test_get_formulation_ingredients_empty_for_incomplete_formulation(app_db_pool, synthetic_catalog):
    ingredients = await get_formulation_ingredients(app_db_pool, synthetic_catalog["formulations"]["incomplete"])
    assert ingredients == []


def test_canonical_ingredient_pair_is_order_independent():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert canonical_ingredient_pair(a, b) == canonical_ingredient_pair(b, a)


async def test_get_active_rules_for_ingredients(app_db_pool, synthetic_catalog):
    rules = await get_active_rules_for_ingredients(app_db_pool, [synthetic_catalog["ingredients"]["retinol"]])
    rule_types = {r["rule_type"] for r in rules}
    assert rule_types == {"PREGNANCY", "NURSING"}


async def test_get_active_rules_for_ingredients_empty_list(app_db_pool, synthetic_catalog):
    assert await get_active_rules_for_ingredients(app_db_pool, []) == []


async def test_get_interactions_within_finds_seeded_pair(app_db_pool, synthetic_catalog):
    interactions = await get_interactions_within(
        app_db_pool,
        [synthetic_catalog["ingredients"]["retinol"], synthetic_catalog["ingredients"]["glycolic_acid"]],
    )
    assert len(interactions) == 1
    assert interactions[0]["interaction_type"] == "INCOMPATIBLE"


async def test_get_interactions_within_requires_both_members_present(app_db_pool, synthetic_catalog):
    """An ingredient that participates in a seeded interaction, queried
    alongside an unrelated ingredient (not its actual interaction
    partner), must not surface that interaction -- only a set
    containing *both* members of a real pair should."""
    interactions = await get_interactions_within(
        app_db_pool,
        [synthetic_catalog["ingredients"]["retinol"], synthetic_catalog["ingredients"]["niacinamide"]],
    )
    assert interactions == []
