"""Phase 6: automatic photo-derived supplement recommendations must not
exist anywhere in the plan output."""
from app.services.plan_service import PlanService


def test_plan_service_has_no_supplement_method():
    assert not hasattr(PlanService, "_build_supplement_recommendations")


def test_generated_plan_has_no_supplement_key():
    service = PlanService()
    scores = {"skin_health_score": 0.3, "vitality_score": 0.5, "feature_definition_score": 0.5, "facial_harmony_score": 0.5}
    insights = {
        "skin_health": {
            "priority_ids": ["EVENNESS_TONE"],
            "priority_severities": {"EVENNESS_TONE": 1.0},
            "influencers": {},
            "overall_score": 0.3,
        },
        "vitality": {"priority_ids": [], "priority_severities": {}, "influencers": {}, "overall_score": 0.5},
        "feature_definition": {"priority_ids": [], "priority_severities": {}, "influencers": {}, "overall_score": 0.5},
        "facial_harmony": {"priority_ids": [], "priority_severities": {}, "influencers": {}, "overall_score": 0.5},
    }
    user_profile = {"user_id": "test", "experience_level": "beginner"}

    plan = service.generate_plan(scores, insights, user_profile, capture_quality=1.0)

    assert "supplement_recommendations" not in plan
