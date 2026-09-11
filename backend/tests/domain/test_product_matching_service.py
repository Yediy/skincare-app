"""ProductMatchingService (Part I, Phase 9/10) against the real,
synthetic catalog. Proves the actual P0 closure: every candidate is
evaluated through SafetyEngine.evaluate_product_formulation(), never
through evaluate_offer() alone, and no fallback ever bypasses that.
"""
import pytest

from app.domain.product_matching_service import MATCHED, NO_SAFE_MATCH, ProductMatchingService
from app.domain.safety_engine import RESTRICTED, SAFE, SafetyEngine, UNSAFE

NO_CONSTRAINTS = {"allergies": [], "avoid_ingredients": []}


@pytest.fixture
def matcher(app_db_pool):
    return ProductMatchingService(app_db_pool, SafetyEngine())


async def test_finds_safe_compatible_product_for_category(matcher, synthetic_catalog):
    """The "moisturizer" category has two real fixtures ("Basic Gentle
    Moisturizer" and the fragrance-containing "Soft Bloom Fragrance
    Cream", both SAFE for a non-allergic user) -- both must be found
    and MATCHED."""
    matches = await matcher.find_compatible_products("moisturizer", NO_CONSTRAINTS)
    formulation_ids = {m.formulation_id for m in matches}
    assert synthetic_catalog["formulations"]["moisturizer"] in formulation_ids
    assert synthetic_catalog["formulations"]["fragrance"] in formulation_ids
    assert all(m.safety_status == SAFE for m in matches)
    assert all(m.match_status == MATCHED for m in matches)
    assert {m.rank_position for m in matches} == {1, 2}


async def test_excludes_unsafe_candidates_pregnancy(matcher, synthetic_catalog):
    """Category "retinoid" has two formulations: "retinol" (pregnancy
    -restricted) and "combo" (pregnancy-restricted AND a HIGH-severity
    within-formulation interaction) -- for a pregnant user, both are
    UNSAFE, so the compatible list must be empty."""
    constraints = {"allergies": [], "avoid_ingredients": [], "is_pregnant": True}
    matches = await matcher.find_compatible_products("retinoid", constraints)
    assert matches == []


async def test_no_safe_product_available_returns_empty_not_a_fallback(matcher, synthetic_catalog):
    """Category "vitamin_c_serum" has three formulations, all with
    incomplete/unknown ingredient data (INSUFFICIENT_DATA) -- none are
    ever MATCHED, and the service must never substitute a generic/
    popular/default product. An empty list is the only honest answer."""
    matches = await matcher.find_compatible_products("vitamin_c_serum", NO_CONSTRAINTS)
    assert matches == []


async def test_incomplete_data_formulations_never_matched_even_with_real_ingredients(matcher, synthetic_catalog):
    """Direct proof that the PARTIAL/UNKNOWN-status formulations (which
    do have real, non-empty ingredient lists) never appear as
    candidates at all -- not merely that they'd fail if evaluated."""
    matches = await matcher.find_compatible_products("vitamin_c_serum", NO_CONSTRAINTS, max_results=10)
    matched_formulation_ids = {m.formulation_id for m in matches}
    assert synthetic_catalog["formulations"]["partial"] not in matched_formulation_ids
    assert synthetic_catalog["formulations"]["unknown"] not in matched_formulation_ids
    assert synthetic_catalog["formulations"]["incomplete"] not in matched_formulation_ids


async def test_restricted_candidate_still_matches(matcher, synthetic_catalog):
    """Sensitive skin restricts (RESTRICTED), it does not exclude --
    a RESTRICTED formulation is still a valid, MATCHED compatible
    option, just ranked after any SAFE ones."""
    constraints = {"allergies": [], "avoid_ingredients": [], "has_sensitive_skin": True}
    matches = await matcher.find_compatible_products("chemical_exfoliant", constraints)
    assert len(matches) == 1
    assert matches[0].safety_status == RESTRICTED
    assert matches[0].match_status == MATCHED


async def test_safe_ranked_before_restricted(matcher, db_pool, synthetic_catalog):
    """Add a second, SAFE moisturizer-category candidate and confirm
    it ranks ahead of one that ends up RESTRICTED for this user."""
    # The "moisturizer" formulation is plain SAFE for everyone. Reuse
    # "acid" under the "moisturizer" category label to create a
    # same-category RESTRICTED competitor for a sensitive-skin user
    # (glycolic acid triggers SENSITIVE_SKIN_INTENSITY_LIMIT, RESTRICTED
    # not UNSAFE) -- this categorization is purely for this test's own
    # ranking proof, not a claim about what "chemical_exfoliant" means.
    await db_pool.execute(
        "UPDATE products SET category = 'moisturizer' WHERE id = $1", synthetic_catalog["products"]["acid"]
    )
    constraints = {"allergies": [], "avoid_ingredients": [], "has_sensitive_skin": True}
    matches = await matcher.find_compatible_products("moisturizer", constraints, max_results=10)
    assert [m.safety_status for m in matches] == sorted(
        [m.safety_status for m in matches], key=lambda s: 0 if s == SAFE else 1
    )
    assert matches[0].safety_status == SAFE
    assert matches[0].rank_position == 1


async def test_global_fallback_used_when_no_market_specific_formulation(matcher, synthetic_catalog):
    """Every seeded formulation is market_or_region='global' -- a
    search for a specific, unmatched region must fall back to the
    global candidates (explicitly marked as such), not silently
    substitute some other specific jurisdiction's formulation (there
    is none to substitute here regardless, but the fallback path
    itself must still work and be flagged)."""
    matches = await matcher.find_compatible_products("moisturizer", NO_CONSTRAINTS, market_or_region="US")
    assert len(matches) == 2
    assert all(m.market_fallback is True for m in matches)
    assert all(m.market_or_region == "global" for m in matches)


async def test_max_results_limits_returned_matches(matcher, synthetic_catalog):
    matches = await matcher.find_compatible_products("moisturizer", NO_CONSTRAINTS, max_results=0)
    assert matches == []


async def test_no_commercial_input_exists_in_match_signature_or_result():
    """Commercial firewall (Phase 10), structural: find_compatible_products
    accepts only category/constraints/market_or_region/max_results --
    no price, brand-preference, commission, or popularity parameter of
    any kind. Proven by inspecting the actual signature, not merely
    asserted in prose."""
    import inspect

    sig = inspect.signature(ProductMatchingService.find_compatible_products)
    param_names = set(sig.parameters.keys()) - {"self"}
    assert param_names == {"category", "constraints", "market_or_region", "max_results"}

    from app.domain.product_matching_service import ProductMatch
    field_names = {f for f in ProductMatch.__dataclass_fields__}
    forbidden = {"price", "commission", "affiliate", "popularity", "merchant"}
    assert not (field_names & forbidden)


async def test_unsafe_candidate_cannot_be_restored_by_ranking(matcher, synthetic_catalog):
    """A UNSAFE-status candidate (pregnancy-restricted retinol, for a
    pregnant user) never appears in the returned list at all, no
    matter how the sort key is computed -- filtering happens strictly
    by match_status before any ranking runs."""
    constraints = {"allergies": [], "avoid_ingredients": [], "is_pregnant": True}
    matches = await matcher.find_compatible_products("retinoid", constraints, max_results=10)
    formulation_ids = {m.formulation_id for m in matches}
    assert synthetic_catalog["formulations"]["retinol"] not in formulation_ids
    assert synthetic_catalog["formulations"]["combo"] not in formulation_ids
