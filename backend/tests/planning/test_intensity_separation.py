"""Phase 15: a beginner's advanced-default concern stays visible; only
the intervention intensity is softened. The old behavior dropped the
priority entirely."""
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


def test_beginner_keeps_advanced_default_concern_visible():
    """TEXTURE_SMOOTHING has default_intensity="advanced". A beginner
    must still see it as a concern -- the old code dropped it entirely."""
    service = PlanService()
    insights = _insights_for(["TEXTURE_SMOOTHING"])
    profile = {"experience_level": "beginner"}

    plan = service.generate_plan(SCORES, insights, profile)

    assert "TEXTURE_SMOOTHING" in plan["metadata"]["all_priority_ids"]


def test_beginner_gets_softened_intensity_not_dropped_concern():
    service = PlanService()
    insights = _insights_for(["TEXTURE_SMOOTHING"])
    profile = {"experience_level": "beginner"}

    plan = service.generate_plan(SCORES, insights, profile)

    assert plan["metadata"]["priority_intensities"]["TEXTURE_SMOOTHING"] == "beginner"

    top = next(p for p in plan["top_priorities"] if p["id"] == "TEXTURE_SMOOTHING")
    assert top["default_intensity"] == "advanced"
    assert top["effective_intensity"] == "beginner"

    # And the routine text itself reflects the softened intensity, not
    # the aggressive default schedule.
    retinoid_step = next(s for s in plan["pm_routine"] if s["product_category"] == "retinoid")
    assert "every other night" in retinoid_step["action"]


def test_advanced_user_gets_full_intensity_unchanged():
    service = PlanService()
    insights = _insights_for(["TEXTURE_SMOOTHING"])
    profile = {"experience_level": "advanced"}

    plan = service.generate_plan(SCORES, insights, profile)

    assert plan["metadata"]["priority_intensities"]["TEXTURE_SMOOTHING"] == "advanced"
    top = next(p for p in plan["top_priorities"] if p["id"] == "TEXTURE_SMOOTHING")
    assert top["effective_intensity"] == "advanced"
