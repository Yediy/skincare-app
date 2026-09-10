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
    INCOMPLETE_FORMULATION_DATA,
    INSUFFICIENT_DATA,
    MAX_FREQUENCY_EXCEEDED,
    NURSING_RESTRICTION,
    PREGNANCY_RESTRICTION,
    RESTRICTED,
    SAFE,
    SafetyEngine,
    SENSITIVE_SKIN_INTENSITY_LIMIT,
    UNKNOWN_FORMULATION,
    UNRESOLVED_ALLERGY_CONSTRAINT,
    UNRESOLVED_AVOID_CONSTRAINT,
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


# --- Part I, Phase 1/2: formulation data completeness -----------------

async def test_partial_formulation_never_appears_safe(app_db_pool, synthetic_catalog):
    """Real ingredients are recorded (unlike the zero-ingredient
    'incomplete' case) but ingredient_data_status is PARTIAL -- must
    still be INSUFFICIENT_DATA, never SAFE, purely because of that
    status field."""
    engine = SafetyEngine()
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["partial"], NO_CONSTRAINTS,
    )
    assert decision.status == INSUFFICIENT_DATA
    assert decision.allowed is False
    assert INCOMPLETE_FORMULATION_DATA in decision.reason_codes
    assert decision.status != SAFE


async def test_unknown_status_formulation_never_appears_safe(app_db_pool, synthetic_catalog):
    engine = SafetyEngine()
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["unknown"], NO_CONSTRAINTS,
    )
    assert decision.status == INSUFFICIENT_DATA
    assert decision.allowed is False
    assert INCOMPLETE_FORMULATION_DATA in decision.reason_codes


async def test_one_or_more_ingredients_is_not_sufficient_for_complete(app_db_pool, synthetic_catalog):
    """A formulation is never inferred COMPLETE merely because it has
    ingredients recorded -- both the partial and unknown fixtures have
    two real ingredients each (same as several SAFE fixtures) and are
    still correctly rejected, proving the gate is the explicit status
    field, not ingredient-list non-emptiness."""
    partial = await SafetyEngine().evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["partial"], NO_CONSTRAINTS,
    )
    unknown = await SafetyEngine().evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["unknown"], NO_CONSTRAINTS,
    )
    assert partial.status == unknown.status == INSUFFICIENT_DATA


# --- Part I, Phase 4: unresolved user constraints ----------------------

async def test_unresolved_allergy_constraint_blocks_specific_product_recommendation(app_db_pool, synthetic_catalog):
    """Even a formulation that would otherwise be SAFE must fail
    closed to INSUFFICIENT_DATA when the calling user has an
    unresolved allergy constraint -- the system must not silently
    recommend a specific product as though the unresolved constraint
    didn't exist."""
    engine = SafetyEngine()
    constraints = {
        "allergies": [], "avoid_ingredients": [],
        "has_unresolved_allergy_constraint": True,
    }
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["moisturizer"], constraints,
    )
    assert decision.status == INSUFFICIENT_DATA
    assert decision.allowed is False
    assert UNRESOLVED_ALLERGY_CONSTRAINT in decision.reason_codes


async def test_unresolved_avoid_constraint_blocks_specific_product_recommendation(app_db_pool, synthetic_catalog):
    engine = SafetyEngine()
    constraints = {
        "allergies": [], "avoid_ingredients": [],
        "has_unresolved_avoid_constraint": True,
    }
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["moisturizer"], constraints,
    )
    assert decision.status == INSUFFICIENT_DATA
    assert decision.allowed is False
    assert UNRESOLVED_AVOID_CONSTRAINT in decision.reason_codes


async def test_no_unresolved_constraint_flags_does_not_block_recommendation(app_db_pool, synthetic_catalog):
    """The absence of the new optional constraint keys (the pre-
    existing calling convention, before this pass) must not itself
    trigger the new gate -- confirms backward compatibility of the
    constraints dict shape."""
    engine = SafetyEngine()
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["moisturizer"], NO_CONSTRAINTS,
    )
    assert decision.status == SAFE


# --- Part I, Phase 5: MAX_FREQUENCY is a restriction, not a verdict ----

async def test_max_frequency_rule_is_surfaced_as_restriction_not_exceeded(app_db_pool, synthetic_catalog):
    """A MAX_FREQUENCY rule existing on an ingredient must never, by
    itself, produce MAX_FREQUENCY_EXCEEDED at the formulation level --
    only evaluate_routine_safety(), once an actual proposed schedule
    is compared against it, can honestly say that."""
    engine = SafetyEngine()
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["retinol"], NO_CONSTRAINTS,
    )
    assert MAX_FREQUENCY_EXCEEDED not in decision.reason_codes
    caps = decision.restrictions.get("ingredient_frequency_caps", [])
    assert any(c["maximum_weekly_frequency"] == 3 for c in caps)


# --- Part I, Phase 6: BARRIER_RECOVERY is inert without active context -

async def test_barrier_recovery_rule_is_never_a_conflict_at_formulation_level(app_db_pool, synthetic_catalog):
    """evaluate_product_formulation() has no usage-context input at
    all -- a BARRIER_RECOVERY rule existing on an ingredient must never
    produce BARRIER_RECOVERY_CONFLICT here, regardless of any
    constraint flag, since formulation-level evaluation must never
    invent whether barrier recovery is currently active."""
    from app.domain.safety_engine import BARRIER_RECOVERY_CONFLICT

    engine = SafetyEngine()
    decision = await engine.evaluate_product_formulation(
        app_db_pool, synthetic_catalog["formulations"]["acid"], NO_CONSTRAINTS,
    )
    assert BARRIER_RECOVERY_CONFLICT not in decision.reason_codes
    rules = decision.restrictions.get("barrier_recovery_rules", [])
    assert len(rules) == 1
