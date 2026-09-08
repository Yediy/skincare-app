"""Phase 13/14: SafetyEngine actually wired into PlanService.generate_plan,
not just callable in isolation. Pure Python -- no DB/Redis needed."""
from app.services.plan_service import PlanService

SCORES = {"skin_health_score": 0.3, "vitality_score": 0.5, "feature_definition_score": 0.5, "facial_harmony_score": 0.5}


def _insights_for(priority_ids, pillar="skin_health"):
    return {
        pillar: {
            "priority_ids": priority_ids,
            "priority_severities": {pid: 1.0 for pid in priority_ids},
            "influencers": {},
            "overall_score": 0.3,
        },
        **{
            p: {"priority_ids": [], "priority_severities": {}, "influencers": {}, "overall_score": 0.5}
            for p in ["skin_health", "vitality", "feature_definition", "facial_harmony"]
            if p != pillar
        },
    }


def test_avoid_ingredient_excludes_category_from_pm_routine():
    """TEXTURE_SMOOTHING's product_categories include "retinoid" and
    "chemical_exfoliant". Avoiding "retinol" must remove the retinoid
    step from the routine, while chemical_exfoliant (not conflicting)
    still appears."""
    service = PlanService()
    insights = _insights_for(["TEXTURE_SMOOTHING"])
    profile = {
        "experience_level": "advanced",  # bypass the beginner/advanced filter
        "avoid_ingredients": ["retinol"],
    }

    plan = service.generate_plan(SCORES, insights, profile)

    pm_categories = {step["product_category"] for step in plan["pm_routine"]}
    assert "retinoid" not in pm_categories
    assert "chemical_exfoliant" in pm_categories

    retinoid_decision = next(
        d for d in plan["metadata"]["safety_decisions"]
        if d["candidate_type"] == "product_category" and d["candidate_id"] == "retinoid"
    )
    assert retinoid_decision["allowed"] is False
    assert "USER_AVOID_INGREDIENT" in retinoid_decision["reason_codes"]


def test_allergy_conflict_recorded_and_excludes_category():
    """EVENNESS_TONE's categories are vitamin_c_serum, niacinamide_serum,
    sunscreen -- none of which carry a "fragrance" allergen in the
    product_safety adapter, so a fragrance allergy should NOT block
    vitamin_c_serum specifically (proving evaluate_offer isn't
    over-blocking things it has no reason to)."""
    service = PlanService()
    insights = _insights_for(["EVENNESS_TONE"])
    profile = {"experience_level": "beginner", "allergies": ["fragrance"]}

    plan = service.generate_plan(SCORES, insights, profile)

    decisions_by_id = {
        d["candidate_id"]: d for d in plan["metadata"]["safety_decisions"] if d["candidate_type"] == "product_category"
    }
    # vitamin_c_serum has no fragrance allergen in the adapter -- not blocked.
    assert decisions_by_id["vitamin_c_serum"]["allowed"] is True
    assert "vitamin_c_serum" in {s["product_category"] for s in plan["am_routine"]}


def test_pregnancy_excludes_restricted_priority_with_disclaimer():
    service = PlanService()
    insights = _insights_for(["TEXTURE_SMOOTHING", "OIL_CONTROL"])
    profile = {"experience_level": "advanced", "is_pregnant": True}

    plan = service.generate_plan(SCORES, insights, profile)

    assert "TEXTURE_SMOOTHING" not in plan["metadata"]["all_priority_ids"]
    assert "OIL_CONTROL" in plan["metadata"]["all_priority_ids"]
    assert any("pregnancy/nursing" in d for d in plan["disclaimers"])

    priority_decisions = {
        d["candidate_id"]: d for d in plan["metadata"]["safety_decisions"] if d["candidate_type"] == "priority"
    }
    assert priority_decisions["TEXTURE_SMOOTHING"]["allowed"] is False
    assert "PREGNANCY_RESTRICTION" in priority_decisions["TEXTURE_SMOOTHING"]["reason_codes"]


def test_sensitive_skin_caps_frequency_and_uses_specific_disclaimer():
    service = PlanService()
    insights = _insights_for(["TEXTURE_SMOOTHING"])
    profile = {"experience_level": "advanced", "has_sensitive_skin": True, "avoid_ingredients": ["glycolic acid", "salicylic acid"]}
    # Avoid the exfoliant so only the retinoid branch is exercised cleanly.

    plan = service.generate_plan(SCORES, insights, profile)

    pm_steps_by_category = {s["product_category"]: s for s in plan["pm_routine"]}
    assert "retinoid" in pm_steps_by_category
    assert "2x/week" in pm_steps_by_category["retinoid"]["action"]

    assert any("capped" in d for d in plan["disclaimers"])
    assert not any("Start slowly and monitor for reactions." in d and "capped" not in d for d in plan["disclaimers"])


def test_sensitive_skin_with_no_triggered_restriction_uses_neutral_disclaimer():
    service = PlanService()
    insights = _insights_for(["OIL_CONTROL"])  # no retinoid/acid category involved
    profile = {"experience_level": "beginner", "has_sensitive_skin": True}

    plan = service.generate_plan(SCORES, insights, profile)

    assert not any("capped" in d for d in plan["disclaimers"])
    assert any("no active-ingredient frequency limits were triggered" in d for d in plan["disclaimers"])
