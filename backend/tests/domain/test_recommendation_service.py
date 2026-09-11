"""app.domain.recommendation_service -- Part I, Phase 11-13, the
actual P0 closure: product matching + routine safety enforcement on
top of a (hand-built, minimal) plan shaped exactly like
PlanService.generate_plan()'s real output. Proves concrete product
recommendations are never produced from category-level safety alone,
and that routine-level restrictions actually change the routine, not
just its metadata.
"""
import json

import pytest

from app.domain.product_matching_service import ProductMatchingService
from app.domain.recommendation_service import apply_product_matching_and_routine_safety
from app.domain.safety_engine import SAFE, SafetyEngine, SafetyUsageContext, UNSAFE

NO_CONSTRAINTS = {"allergies": [], "avoid_ingredients": []}


async def _add_formulation_with_ingredient_rule(
    db_pool, brand_id, *, category, rule_type, action, cap=None, suffix="",
):
    """Test-local catalog insert: one fresh ingredient + product +
    COMPLETE formulation carrying exactly one ingredient_rules row of
    the given (rule_type, action). Isolated from synthetic_catalog's
    own shared rows -- this pass's P0 fix needs EXCLUDE-action
    MAX_FREQUENCY/BARRIER_RECOVERY rules, which synthetic_catalog's
    existing retinol/glycolic_acid rows deliberately do not have
    (other tests assert their RESTRICT-action behavior)."""
    label = f"{rule_type}_{action}_{suffix}"
    async with db_pool.acquire() as conn:
        ingredient_id = await conn.fetchval(
            "INSERT INTO ingredients (canonical_name, normalized_name, ingredient_type) "
            "VALUES ($1, $2, 'active') RETURNING id",
            f"Test {label}", f"test {label}".lower(),
        )
        product_id = await conn.fetchval(
            "INSERT INTO products (brand_id, name, normalized_name, category) VALUES ($1, $2, $3, $4) RETURNING id",
            brand_id, f"Test {label} Product", f"test {label} product".lower(), category,
        )
        formulation_id = await conn.fetchval(
            """
            INSERT INTO product_formulations
                (product_id, version, source_type, verified_at, ingredient_data_status, market_or_region)
            VALUES ($1, '1', 'manufacturer_disclosure', now(), 'COMPLETE', 'global')
            RETURNING id
            """,
            product_id,
        )
        await conn.execute(
            "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, $3)",
            product_id, formulation_id, f"TEST-{label}-{formulation_id}",
        )
        await conn.execute(
            "INSERT INTO formulation_ingredients (formulation_id, ingredient_id, position) VALUES ($1, $2, 1)",
            formulation_id, ingredient_id,
        )
        params = {"maximum_weekly_frequency": cap} if cap is not None else {}
        await conn.execute(
            "INSERT INTO ingredient_rules (ingredient_id, rule_type, severity, action, reason_code, parameters) "
            "VALUES ($1, $2, 'HIGH', $3, $4, $5::jsonb)",
            ingredient_id, rule_type, action, f"{label}_REASON"[:50], json.dumps(params),
        )
    return ingredient_id, product_id, formulation_id


async def _add_formulation_with_internal_exclude_combination(db_pool, brand_id, *, category, suffix=""):
    """One formulation whose own two ingredients carry a MODERATE-
    severity EXCLUDE_COMBINATION interaction. MODERATE (not HIGH/
    CRITICAL) deliberately: evaluate_product_formulation() only marks
    a formulation UNSAFE for an internal interaction at HIGH/CRITICAL
    severity, so this formulation passes formulation-level safety
    (RESTRICTED, matched) despite carrying an interaction pair whose
    recommended_action is EXCLUDE_COMBINATION -- the real-world case
    where routine-level evaluation, not formulation-level, is what
    actually catches it, and pairwise conflict resolution has no
    'other side' step to drop it in favor of."""
    async with db_pool.acquire() as conn:
        ing_a = await conn.fetchval(
            "INSERT INTO ingredients (canonical_name, normalized_name, ingredient_type) "
            "VALUES ($1, $2, 'active') RETURNING id",
            f"Combo Ingredient A {suffix}", f"combo ingredient a {suffix}".lower(),
        )
        ing_b = await conn.fetchval(
            "INSERT INTO ingredients (canonical_name, normalized_name, ingredient_type) "
            "VALUES ($1, $2, 'active') RETURNING id",
            f"Combo Ingredient B {suffix}", f"combo ingredient b {suffix}".lower(),
        )
        a_id, b_id = (ing_a, ing_b) if str(ing_a) < str(ing_b) else (ing_b, ing_a)
        await conn.execute(
            """
            INSERT INTO ingredient_interactions
                (ingredient_a_id, ingredient_b_id, interaction_type, severity, reason_code, recommendation, recommended_action)
            VALUES ($1, $2, 'INCOMPATIBLE', 'MODERATE', 'UNRESOLVABLE_TEST_COMBO', 'test-only', 'EXCLUDE_COMBINATION')
            """,
            a_id, b_id,
        )
        product_id = await conn.fetchval(
            "INSERT INTO products (brand_id, name, normalized_name, category) VALUES ($1, $2, $3, $4) RETURNING id",
            brand_id, f"Combo Product {suffix}", f"combo product {suffix}".lower(), category,
        )
        formulation_id = await conn.fetchval(
            """
            INSERT INTO product_formulations
                (product_id, version, source_type, verified_at, ingredient_data_status, market_or_region)
            VALUES ($1, '1', 'manufacturer_disclosure', now(), 'COMPLETE', 'global')
            RETURNING id
            """,
            product_id,
        )
        await conn.execute(
            "INSERT INTO product_skus (product_id, formulation_id, sku) VALUES ($1, $2, $3)",
            product_id, formulation_id, f"TEST-COMBO-{suffix}-{formulation_id}",
        )
        for position, ingredient_id in enumerate((ing_a, ing_b), start=1):
            await conn.execute(
                "INSERT INTO formulation_ingredients (formulation_id, ingredient_id, position) VALUES ($1, $2, $3)",
                formulation_id, ingredient_id, position,
            )
    return formulation_id


def _minimal_plan(am_categories=(), pm_categories=()):
    def _steps(categories, offset=0):
        return [
            {
                "step_number": i + 1 + offset,
                "product_category": cat,
                "action": "apply", "why": "test", "priority": "standard",
                "product_recommendations": [],
            }
            for i, cat in enumerate(categories)
        ]

    return {
        "am_routine": _steps(am_categories),
        "pm_routine": _steps(pm_categories),
        "metadata": {"safety_decisions": []},
    }


@pytest.fixture
def matcher(app_db_pool):
    return ProductMatchingService(app_db_pool, SafetyEngine())


async def test_compatible_product_gets_attached_to_its_step(app_db_pool, matcher, synthetic_catalog):
    plan = _minimal_plan(am_categories=["moisturizer"])
    result = await apply_product_matching_and_routine_safety(
        app_db_pool, plan, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
    )
    step = result.plan["am_routine"][0]
    assert len(step["product_recommendations"]) >= 1
    assert step["product_recommendations"][0]["safety_status"] == SAFE
    assert result.routine_safety_decision.status == SAFE
    assert len(result.product_recommendations) == 1
    assert result.product_recommendations[0].plan_step_key == "AM:1"


async def test_no_compatible_product_leaves_step_empty_not_a_fallback(app_db_pool, matcher, synthetic_catalog):
    """Category "vitamin_c_serum" has only incomplete/unknown-data
    formulations (see synthetic_catalog) -- the step must keep an
    empty product_recommendations list, never a generic substitute."""
    plan = _minimal_plan(am_categories=["vitamin_c_serum"])
    result = await apply_product_matching_and_routine_safety(
        app_db_pool, plan, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
    )
    assert result.plan["am_routine"][0]["product_recommendations"] == []
    assert result.product_recommendations == []


async def test_cross_product_exclude_combination_drops_the_later_step(app_db_pool, matcher, synthetic_catalog):
    """retinol (AM) and glycolic acid (PM) have a real seeded
    EXCLUDE_COMBINATION interaction -- Phase 12 requires the actual
    routine to obey it. AM is ordered before PM, so PM's specific
    product must be dropped (falls back to its abstract category, no
    specific product), and the final routine safety decision must no
    longer be UNSAFE."""
    plan = _minimal_plan(am_categories=["retinoid"], pm_categories=["chemical_exfoliant"])
    result = await apply_product_matching_and_routine_safety(
        app_db_pool, plan, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
    )
    assert result.routine_safety_decision.status != UNSAFE
    am_step = result.plan["am_routine"][0]
    pm_step = result.plan["pm_routine"][0]
    assert len(am_step["product_recommendations"]) >= 1  # AM (earlier) keeps its match
    assert pm_step["product_recommendations"] == []  # PM (later) yields


async def test_max_frequency_cap_is_enforced_on_the_actual_step(app_db_pool, matcher, synthetic_catalog):
    """The retinol formulation's MAX_FREQUENCY rule caps at 3/week.
    A "retinoid" category step alone (no interaction conflict) must
    end up with enforced_weekly_frequency == 3 once the default
    proposed frequency (also 3, per this module's own active-ingredient
    default) is compared -- to prove the mechanism fires even at the
    boundary, this test forces a higher default by using a category
    with no existing restriction data, relying on the module's
    ACTIVE_INGREDIENT_DEFAULT_WEEKLY_FREQUENCY landing exactly on the
    rule's cap (3), then confirms the cap value itself is correctly
    read from the rule, not hardcoded."""
    plan = _minimal_plan(am_categories=["retinoid"])
    result = await apply_product_matching_and_routine_safety(
        app_db_pool, plan, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
    )
    # Default proposed frequency (3) does not exceed the cap (3) --
    # confirm no cap is enforced in that boundary case, then explicitly
    # push over it via a category-level restriction to prove the wiring.
    assert "enforced_weekly_frequency" not in result.plan["am_routine"][0]

    plan2 = _minimal_plan(am_categories=["retinoid"])
    plan2["metadata"]["safety_decisions"] = [
        {"candidate_type": "product_category", "candidate_id": "retinoid",
         "restrictions": {"maximum_weekly_frequency": 5}},
    ]
    result2 = await apply_product_matching_and_routine_safety(
        app_db_pool, plan2, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
    )
    assert result2.plan["am_routine"][0]["enforced_weekly_frequency"] == 3


async def test_barrier_recovery_active_context_is_threaded_through(app_db_pool, matcher, synthetic_catalog):
    plan = _minimal_plan(am_categories=["chemical_exfoliant"])
    result = await apply_product_matching_and_routine_safety(
        app_db_pool, plan, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
        usage_context=SafetyUsageContext(barrier_recovery_active=True),
    )
    from app.domain.safety_engine import BARRIER_RECOVERY_CONFLICT
    assert BARRIER_RECOVERY_CONFLICT in result.routine_safety_decision.reason_codes


async def test_product_recommendation_provenance_is_complete(app_db_pool, matcher, synthetic_catalog):
    """Phase 13: every field required to reconstruct the
    recommendation later is actually present."""
    plan = _minimal_plan(am_categories=["moisturizer"])
    result = await apply_product_matching_and_routine_safety(
        app_db_pool, plan, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
    )
    rec = result.product_recommendations[0]
    d = rec.to_dict()
    for field_name in (
        "plan_step_key", "product_id", "formulation_id", "brand", "product_name",
        "rank_position", "safety_status", "reason_codes", "restrictions", "rules_version",
    ):
        assert field_name in d


# --- P0 safety fix (this pass): fail closed on UNSAFE routine -------------
#
# Central invariant under test in every case below: routine_decision.status
# == UNSAFE must never coexist with a non-empty product_recommendations for
# the implicated step(s) -- never merely recorded as metadata while the
# concrete (unsafe) product is still returned.

async def test_max_frequency_exclude_action_removes_unsafe_product(app_db_pool, db_pool, matcher, synthetic_catalog):
    """A MAX_FREQUENCY rule with action EXCLUDE (not RESTRICT) makes the
    routine UNSAFE once the proposed schedule exceeds the cap. With
    only one candidate in the category, conflict resolution must drop
    it entirely (no alternative to fall back to) rather than emit it
    -- the step keeps its abstract category, no concrete product."""
    _, _, formulation_id = await _add_formulation_with_ingredient_rule(
        db_pool, synthetic_catalog["brand_id"], category="strict_retinoid_solo",
        rule_type="MAX_FREQUENCY", action="EXCLUDE", cap=1, suffix="solo",
    )
    plan = _minimal_plan(am_categories=["strict_retinoid_solo"])
    result = await apply_product_matching_and_routine_safety(
        app_db_pool, plan, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
    )
    step = result.plan["am_routine"][0]
    assert step["product_recommendations"] == []
    assert result.product_recommendations == []
    assert result.routine_safety_decision.status != UNSAFE
    assert result.plan["metadata"]["unresolved_unsafe_routine"] is False
    # The unsafe formulation itself must never appear anywhere in the
    # final output, concrete-recommendation-shaped or otherwise.
    assert str(formulation_id) not in json.dumps(result.plan["am_routine"])


async def test_barrier_recovery_exclude_action_removes_unsafe_product(app_db_pool, db_pool, matcher, synthetic_catalog):
    """Same invariant, for a BARRIER_RECOVERY rule with action EXCLUDE,
    while barrier_recovery_active is True."""
    _, _, formulation_id = await _add_formulation_with_ingredient_rule(
        db_pool, synthetic_catalog["brand_id"], category="strict_barrier_solo",
        rule_type="BARRIER_RECOVERY", action="EXCLUDE", suffix="solo",
    )
    plan = _minimal_plan(am_categories=["strict_barrier_solo"])
    result = await apply_product_matching_and_routine_safety(
        app_db_pool, plan, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
        usage_context=SafetyUsageContext(barrier_recovery_active=True),
    )
    step = result.plan["am_routine"][0]
    assert step["product_recommendations"] == []
    assert result.product_recommendations == []
    assert result.routine_safety_decision.status != UNSAFE
    assert str(formulation_id) not in json.dumps(result.plan["am_routine"])


async def test_max_frequency_exclude_exhausts_multiple_alternatives(app_db_pool, db_pool, matcher, synthetic_catalog):
    """Two different formulations in the same category, both carrying
    an EXCLUDE-action MAX_FREQUENCY rule that the proposed schedule
    exceeds. Conflict resolution must drop the top choice, then the
    next, exhausting every alternative -- never emitting either one --
    and still terminate with the routine not UNSAFE (the category
    simply has no safe concrete product for this schedule)."""
    _, _, fid1 = await _add_formulation_with_ingredient_rule(
        db_pool, synthetic_catalog["brand_id"], category="strict_retinoid_multi",
        rule_type="MAX_FREQUENCY", action="EXCLUDE", cap=1, suffix="alt1",
    )
    _, _, fid2 = await _add_formulation_with_ingredient_rule(
        db_pool, synthetic_catalog["brand_id"], category="strict_retinoid_multi",
        rule_type="MAX_FREQUENCY", action="EXCLUDE", cap=1, suffix="alt2",
    )
    plan = _minimal_plan(am_categories=["strict_retinoid_multi"])
    result = await apply_product_matching_and_routine_safety(
        app_db_pool, plan, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
    )
    step = result.plan["am_routine"][0]
    assert step["product_recommendations"] == []
    assert result.product_recommendations == []
    assert result.routine_safety_decision.status != UNSAFE
    assert result.plan["metadata"]["unresolved_unsafe_routine"] is False
    dumped = json.dumps(result.plan["am_routine"])
    assert str(fid1) not in dumped
    assert str(fid2) not in dumped


async def test_unresolved_exclude_combination_clears_entire_routine(app_db_pool, db_pool, matcher, synthetic_catalog):
    """A formulation whose own two ingredients carry a MODERATE-
    severity EXCLUDE_COMBINATION interaction passes FORMULATION-level
    safety (RESTRICTED, not UNSAFE -- only HIGH/CRITICAL trips
    formulation-level unsafe) but makes the ROUTINE unsafe. There is
    no second, later-ordered step to yield to -- pairwise conflict
    resolution cannot attribute this to a droppable pair, so the hard
    backstop must fire: every step's concrete candidates are cleared,
    including the otherwise-fine moisturizer step, and the plan must
    say so explicitly rather than silently returning an all-empty
    routine with no explanation."""
    combo_formulation_id = await _add_formulation_with_internal_exclude_combination(
        db_pool, synthetic_catalog["brand_id"], category="conflicted_combo", suffix="unresolved",
    )
    plan = _minimal_plan(am_categories=["moisturizer", "conflicted_combo"])
    result = await apply_product_matching_and_routine_safety(
        app_db_pool, plan, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
    )
    assert result.routine_safety_decision.status == UNSAFE
    assert result.plan["metadata"]["unresolved_unsafe_routine"] is True
    assert "safety_notice" in result.plan["metadata"]
    # No concrete product anywhere -- not even for the moisturizer step,
    # which was never itself part of the conflict.
    assert result.plan["am_routine"][0]["product_recommendations"] == []
    assert result.plan["am_routine"][1]["product_recommendations"] == []
    assert result.product_recommendations == []
    assert str(combo_formulation_id) not in json.dumps(result.plan["am_routine"])


async def test_unsafe_routine_status_never_coexists_with_concrete_recommendations(
    app_db_pool, db_pool, matcher, synthetic_catalog,
):
    """Direct assertion of the central invariant itself, independent
    of which specific rule/interaction produced UNSAFE: whenever the
    final routine_safety_decision is UNSAFE, product_recommendations
    must be empty -- never both true at once. Exercises the same
    unresolved-combination scenario as the test above, but asserts the
    invariant generically so a future change to the resolution
    heuristic can't silently reintroduce the P0 bug."""
    await _add_formulation_with_internal_exclude_combination(
        db_pool, synthetic_catalog["brand_id"], category="conflicted_combo_2", suffix="invariant",
    )
    plan = _minimal_plan(am_categories=["conflicted_combo_2"])
    result = await apply_product_matching_and_routine_safety(
        app_db_pool, plan, NO_CONSTRAINTS, product_matching_service=matcher, safety_engine=SafetyEngine(),
    )
    if result.routine_safety_decision.status == UNSAFE:
        assert result.product_recommendations == []
        for step in result.plan["am_routine"] + result.plan["pm_routine"]:
            assert step["product_recommendations"] == []
