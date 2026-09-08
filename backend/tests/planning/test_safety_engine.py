"""Phase 12: SafetyEngine and SafetyDecision, tested directly."""
from app.domain.safety_engine import (
    SafetyEngine,
    ALLERGY_CONFLICT,
    USER_AVOID_INGREDIENT,
    SENSITIVE_SKIN_INTENSITY_LIMIT,
    PREGNANCY_RESTRICTION,
    NURSING_RESTRICTION,
    SAFETY_DATA_UNAVAILABLE,
)
from app.domain.priorities import PRIORITIES


def test_evaluate_offer_allows_when_no_conflict():
    engine = SafetyEngine()
    decision = engine.evaluate_offer("vitamin_c_serum", {"allergies": [], "avoid_ingredients": []})
    assert decision.allowed is True
    assert decision.reason_codes == []
    assert decision.candidate_type == "product_category"
    assert decision.candidate_id == "vitamin_c_serum"


def test_evaluate_offer_blocks_allergy_conflict():
    engine = SafetyEngine()
    decision = engine.evaluate_offer("cleanser", {"allergies": ["fragrance"], "avoid_ingredients": []})
    assert decision.allowed is False
    assert ALLERGY_CONFLICT in decision.reason_codes


def test_evaluate_offer_allergy_match_is_case_insensitive_and_trims_whitespace():
    engine = SafetyEngine()
    decision = engine.evaluate_offer("cleanser", {"allergies": ["  Fragrance  "], "avoid_ingredients": []})
    assert decision.allowed is False
    assert ALLERGY_CONFLICT in decision.reason_codes


def test_evaluate_offer_blocks_avoid_ingredient():
    engine = SafetyEngine()
    decision = engine.evaluate_offer("retinoid", {"allergies": [], "avoid_ingredients": ["retinol"]})
    assert decision.allowed is False
    assert USER_AVOID_INGREDIENT in decision.reason_codes


def test_evaluate_offer_pregnancy_restricts_retinoid():
    engine = SafetyEngine()
    decision = engine.evaluate_offer("retinoid", {"allergies": [], "avoid_ingredients": [], "is_pregnant": True})
    assert decision.allowed is False
    assert PREGNANCY_RESTRICTION in decision.reason_codes


def test_evaluate_offer_nursing_restricts_retinoid():
    engine = SafetyEngine()
    decision = engine.evaluate_offer("retinoid", {"allergies": [], "avoid_ingredients": [], "is_nursing": True})
    assert decision.allowed is False
    assert NURSING_RESTRICTION in decision.reason_codes


def test_evaluate_offer_sensitive_skin_restricts_frequency_not_exclusion():
    """Sensitive skin caps frequency -- it does not exclude the
    category outright the way an allergy does."""
    engine = SafetyEngine()
    decision = engine.evaluate_offer("retinoid", {"allergies": [], "avoid_ingredients": [], "has_sensitive_skin": True})
    assert decision.allowed is True
    assert SENSITIVE_SKIN_INTENSITY_LIMIT in decision.reason_codes
    assert decision.restrictions["maximum_weekly_frequency"] == 2


def test_evaluate_offer_unknown_category_fails_closed():
    """Missing safety data must never silently mean safe."""
    engine = SafetyEngine()
    decision = engine.evaluate_offer("totally_unknown_category", {"allergies": [], "avoid_ingredients": []})
    assert decision.allowed is False
    assert decision.reason_codes == [SAFETY_DATA_UNAVAILABLE]


def test_evaluate_priorities_excludes_pregnancy_restricted():
    engine = SafetyEngine()
    priority_ids = ["TEXTURE_SMOOTHING", "OIL_CONTROL"]  # TEXTURE_SMOOTHING is PREGNANCY_RESTRICTED
    decisions = engine.evaluate_priorities(
        priority_ids, {pid: PRIORITIES[pid] for pid in priority_ids}, {"is_pregnant": True}
    )
    by_id = {d.candidate_id: d for d in decisions}
    assert by_id["TEXTURE_SMOOTHING"].allowed is False
    assert PREGNANCY_RESTRICTION in by_id["TEXTURE_SMOOTHING"].reason_codes
    assert by_id["OIL_CONTROL"].allowed is True


def test_safety_decision_to_dict_is_plain_json_serializable():
    engine = SafetyEngine()
    decision = engine.evaluate_offer("cleanser", {"allergies": ["fragrance"], "avoid_ingredients": []})
    import json
    serialized = json.dumps(decision.to_dict())  # must not raise
    assert "ALLERGY_CONFLICT" in serialized
