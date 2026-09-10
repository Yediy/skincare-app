"""Formulation-level safety evaluation (product/usage foundation pass)
-- SafetyEngine.evaluate_product_formulation() against the real,
synthetic catalog (see tests/conftest.py's synthetic_catalog fixture
for exactly what's seeded and why). Proves actual ingredient entities
-- not category strings -- trigger the SafetyDecision, through the
restricted skincare_app role (app_db_pool), the same access path
production actually uses.
"""
import uuid

import pytest

from app.domain.safety_engine import (
    ACTIVE_INTERACTION_CONFLICT,
    ALLERGY_CONFLICT,
    INSUFFICIENT_DATA,
    NURSING_RESTRICTION,
    PREGNANCY_RESTRICTION,
    RESTRICTED,
    SAFE,
    SafetyEngine,
    SENSITIVE_SKIN_INTENSITY_LIMIT,
    UNKNOWN_FORMULATION,
    UNSAFE,
    USER_AVOID_INGREDIENT,
)

NO_CONSTRAINTS = {"allergies": [], "avoid_ingredients": []}


async def test_basic_moisturizer_is_safe(app_db_pool, synthetic_catalog):
    engine = SafetyEngine()
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["moisturizer"], NO_CONSTRAINTS,
    )
    assert decision.status == SAFE
    assert decision.allowed is True
    assert decision.reason_codes == []


async def test_safe_formulation_niacinamide_serum(app_db_pool, synthetic_catalog):
    engine = SafetyEngine()
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["safe_serum"], NO_CONSTRAINTS,
    )
    assert decision.status == SAFE
    assert decision.allowed is True


async def test_fragrance_containing_product_is_safe_for_non_allergic_user(app_db_pool, synthetic_catalog):
    """A fragrance-containing formulation is not inherently unsafe --
    only a real conflict with a real user constraint makes it so."""
    engine = SafetyEngine()
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["fragrance"], NO_CONSTRAINTS,
    )
    assert decision.status == SAFE
    assert decision.allowed is True


async def test_allergen_conflict(app_db_pool, synthetic_catalog):
    engine = SafetyEngine()
    constraints = {"allergies": ["fragrance"], "avoid_ingredients": []}
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["fragrance"], constraints,
    )
    assert decision.status == UNSAFE
    assert decision.allowed is False
    assert ALLERGY_CONFLICT in decision.reason_codes


async def test_user_avoid_ingredient_conflict(app_db_pool, synthetic_catalog):
    engine = SafetyEngine()
    constraints = {"allergies": [], "avoid_ingredients": ["retinol"]}
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["retinol"], constraints,
    )
    assert decision.status == UNSAFE
    assert decision.allowed is False
    assert USER_AVOID_INGREDIENT in decision.reason_codes


async def test_ingredient_alias_conflict(app_db_pool, synthetic_catalog):
    """The user's free-text avoid-ingredient entry ("Vitamin A1") is
    only an *alias* for the formulation's actual ingredient
    (canonical name "Retinol") -- resolution must go through
    ingredient_aliases, not a literal string match, to catch this."""
    engine = SafetyEngine()
    constraints = {"allergies": [], "avoid_ingredients": ["Vitamin A1"]}
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["retinol"], constraints,
    )
    assert decision.status == UNSAFE
    assert USER_AVOID_INGREDIENT in decision.reason_codes


async def test_retinoid_pregnancy_restriction(app_db_pool, synthetic_catalog):
    engine = SafetyEngine()
    constraints = {"allergies": [], "avoid_ingredients": [], "is_pregnant": True}
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["retinol"], constraints,
    )
    assert decision.status == UNSAFE
    assert decision.allowed is False
    assert PREGNANCY_RESTRICTION in decision.reason_codes


async def test_nursing_restriction(app_db_pool, synthetic_catalog):
    engine = SafetyEngine()
    constraints = {"allergies": [], "avoid_ingredients": [], "is_nursing": True}
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["retinol"], constraints,
    )
    assert decision.status == UNSAFE
    assert decision.allowed is False
    assert NURSING_RESTRICTION in decision.reason_codes


async def test_pregnant_user_unaffected_by_a_formulation_with_no_pregnancy_rule(app_db_pool, synthetic_catalog):
    """Pregnancy status alone doesn't taint every formulation --
    only one with an actual PREGNANCY rule on one of its ingredients."""
    engine = SafetyEngine()
    constraints = {"allergies": [], "avoid_ingredients": [], "is_pregnant": True}
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["moisturizer"], constraints,
    )
    assert decision.status == SAFE


async def test_sensitive_skin_restricts_frequency_not_exclusion(app_db_pool, synthetic_catalog):
    """Sensitive skin caps frequency (RESTRICTED) -- it does not
    exclude the formulation outright (UNSAFE) the way an allergy does."""
    engine = SafetyEngine()
    constraints = {"allergies": [], "avoid_ingredients": [], "has_sensitive_skin": True}
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["acid"], constraints,
    )
    assert decision.status == RESTRICTED
    assert decision.allowed is True
    assert SENSITIVE_SKIN_INTENSITY_LIMIT in decision.reason_codes
    assert decision.restrictions["maximum_weekly_frequency"] == 2


async def test_ingredient_interaction_conflict(app_db_pool, synthetic_catalog):
    """A formulation combining two individually-fine ingredients that
    have a real, seeded HIGH-severity interaction between them."""
    engine = SafetyEngine()
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["combo"], NO_CONSTRAINTS,
    )
    assert decision.status == UNSAFE
    assert decision.allowed is False
    assert ACTIVE_INTERACTION_CONFLICT in decision.reason_codes
    assert len(decision.restrictions["interactions"]) == 1
    assert decision.restrictions["interactions"][0]["severity"] == "HIGH"


async def test_incomplete_formulation_is_insufficient_data_not_safe(app_db_pool, synthetic_catalog):
    """A formulation with zero recorded ingredients must never be
    treated as safe -- this is the Unknown Formulation Policy's core
    requirement."""
    engine = SafetyEngine()
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["incomplete"], NO_CONSTRAINTS,
    )
    assert decision.status == INSUFFICIENT_DATA
    assert decision.allowed is False
    assert UNKNOWN_FORMULATION in decision.reason_codes
    assert decision.status != SAFE


async def test_unknown_formulation_id_is_insufficient_data(app_db_pool, synthetic_catalog):
    """A formulation_id that doesn't exist at all in the catalog --
    same policy as the zero-ingredient case: never SAFE."""
    engine = SafetyEngine()
    decision = await engine.evaluate_product_formulation(app_db_pool, uuid.uuid4(), NO_CONSTRAINTS)
    assert decision.status == INSUFFICIENT_DATA
    assert decision.allowed is False
    assert UNKNOWN_FORMULATION in decision.reason_codes


async def test_commercial_factors_cannot_change_safety_result(app_db_pool, synthetic_catalog):
    """The critical invariant: changing brand, product display name,
    or SKU cannot change the safety result for an unchanged
    formulation. 'Rebrand Labs' / 'Premium Renewal Serum' / SKU
    REBRAND-01 is a *different* brand/product/SKU that happens to
    share the exact same underlying formulation as 'Testonyx' /
    'Renewal Night Retinol Serum' / TNX-RETIN-01 (a real-world
    white-label pattern) -- both must resolve to literally the same
    formulation_id via the catalog, and evaluate_product_formulation
    (which never receives brand/price/SKU-text as input at all) must
    return an identical decision for it regardless of which
    product/SKU it was reached through."""
    from app.db.catalog_repository import get_formulation_by_sku

    engine = SafetyEngine()
    constraints = {"allergies": [], "avoid_ingredients": [], "is_pregnant": True}

    original = await get_formulation_by_sku(app_db_pool, "TNX-RETIN-01")
    rebranded = await get_formulation_by_sku(app_db_pool, "REBRAND-01")
    assert original["id"] == rebranded["id"], "both SKUs must resolve to the same underlying formulation"

    decision_original = await engine.evaluate_product_formulation(app_db_pool, original["id"], constraints)
    decision_rebranded = await engine.evaluate_product_formulation(app_db_pool, rebranded["id"], constraints)

    assert decision_original.status == decision_rebranded.status == UNSAFE
    assert decision_original.reason_codes == decision_rebranded.reason_codes
    assert decision_original.restrictions == decision_rebranded.restrictions
