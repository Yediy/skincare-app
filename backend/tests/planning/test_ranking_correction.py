"""Phase 16: display_order must not be able to outrank a real severity
difference -- it's a tie-break only now."""
from app.services.plan_service import PlanService

SCORES = {"skin_health_score": 0.5, "vitality_score": 0.5, "feature_definition_score": 0.5, "facial_harmony_score": 0.5}


def test_higher_severity_wins_even_against_lower_display_order():
    """REDNESS_CONTROL has display_order=2 (worse than EVENNESS_TONE's
    display_order=1), but if REDNESS_CONTROL's real severity is
    substantially higher, it must still rank first. Under the old
    (0.05-per-step) bonus, a 0.1 severity gap could be masked by
    display_order alone -- this proves that can no longer happen."""
    service = PlanService()
    insights = {
        "skin_health": {
            "priority_ids": ["EVENNESS_TONE", "REDNESS_CONTROL"],
            "priority_severities": {"EVENNESS_TONE": 0.20, "REDNESS_CONTROL": 0.35},
            "influencers": {},
            "overall_score": 0.5,
        },
        "vitality": {"priority_ids": [], "priority_severities": {}, "influencers": {}, "overall_score": 0.5},
        "feature_definition": {"priority_ids": [], "priority_severities": {}, "influencers": {}, "overall_score": 0.5},
        "facial_harmony": {"priority_ids": [], "priority_severities": {}, "influencers": {}, "overall_score": 0.5},
    }
    profile = {"experience_level": "advanced"}

    plan = service.generate_plan(SCORES, insights, profile)

    assert plan["metadata"]["all_priority_ids"][0] == "REDNESS_CONTROL"


def test_display_order_only_breaks_near_exact_ties():
    service = PlanService()
    insights = {
        "skin_health": {
            "priority_ids": ["EVENNESS_TONE", "REDNESS_CONTROL"],
            "priority_severities": {"EVENNESS_TONE": 0.30, "REDNESS_CONTROL": 0.30},
            "influencers": {},
            "overall_score": 0.5,
        },
        "vitality": {"priority_ids": [], "priority_severities": {}, "influencers": {}, "overall_score": 0.5},
        "feature_definition": {"priority_ids": [], "priority_severities": {}, "influencers": {}, "overall_score": 0.5},
        "facial_harmony": {"priority_ids": [], "priority_severities": {}, "influencers": {}, "overall_score": 0.5},
    }
    profile = {"experience_level": "advanced"}

    plan = service.generate_plan(SCORES, insights, profile)

    # Equal severity -- EVENNESS_TONE (display_order=1) should edge out
    # REDNESS_CONTROL (display_order=2) via the tiny tie-break only.
    assert plan["metadata"]["all_priority_ids"][0] == "EVENNESS_TONE"
    severities = plan["metadata"]["priority_severities"]
    assert abs(severities["EVENNESS_TONE"] - severities["REDNESS_CONTROL"]) < 0.01
