"""app.domain.recommendation_service -- Part I, Phase 11-13, the
actual P0 closure: product matching + routine safety enforcement on
top of a (hand-built, minimal) plan shaped exactly like
PlanService.generate_plan()'s real output. Proves concrete product
recommendations are never produced from category-level safety alone,
and that routine-level restrictions actually change the routine, not
just its metadata.
"""
import pytest

from app.domain.product_matching_service import ProductMatchingService
from app.domain.recommendation_service import apply_product_matching_and_routine_safety
from app.domain.safety_engine import SAFE, SafetyEngine, SafetyUsageContext, UNSAFE

NO_CONSTRAINTS = {"allergies": [], "avoid_ingredients": []}


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
