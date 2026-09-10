"""STOP GATE A: proves the real, wired production path --
POST /analyze -> PlanService -> ProductMatchingService -> actual
catalog formulation -> SafetyEngine.evaluate_product_formulation() ->
routine-level safety -> enforced routine -- over real HTTP, through the
real restricted skincare_app role, against the real synthetic catalog.

Individual safety scenarios (allergy/avoid/pregnancy/nursing/sensitive-
skin/interaction/incomplete-data/unresolved-constraint/no-safe-match)
are already exhaustively covered at the SafetyEngine/
ProductMatchingService/recommendation_service unit level (tests/
planning/test_formulation_safety.py, test_routine_safety.py, tests/
domain/test_product_matching_service.py, test_recommendation_service.py)
-- not re-derived here through the CV pipeline, which would require
fragile, indirect control over which priorities a real photo scores
into. What this file proves, that unit tests structurally cannot, is
that the real HTTP route actually calls this whole chain for a real
request, not just that the chain works in isolation.

PlanService's AM/PM routine always includes a "cleanser" and a
"moisturizer" (or "light_moisturizer") step unconditionally
(app/services/plan_service.py's essential steps, independent of which
priorities a photo triggers) -- the synthetic catalog's "moisturizer"
-category, SAFE formulation is reachable through every real /analyze
call for that reason, without needing to force a specific detected
concern.
"""
import base64
from pathlib import Path

import pytest

GRACE_HOPPER_JPG = Path(__file__).resolve().parent.parent / "fixtures" / "grace_hopper.jpg"


async def _signup_login_consent(client, email):
    from app.db.consent_repository import REQUIRED_POLICY_VERSION

    await client.post("/signup", json={"email": email, "password": "testpass123"})
    login = await client.post("/login", json={"email": email, "password": "testpass123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    consent = await client.post(
        "/consent",
        json={"policy_version": REQUIRED_POLICY_VERSION, "purpose": "facial skin analysis"},
        headers=headers,
    )
    assert consent.status_code == 200
    return headers


def _find_step_with_recommendations(plan):
    for step in plan["am_routine"] + plan["pm_routine"]:
        if step["product_recommendations"]:
            return step
    return None


async def test_analyze_attaches_real_catalog_product_with_formulation_safety_decision(client, synthetic_catalog):
    """The actual P0 closure, proven end-to-end: a real /analyze call
    returns at least one routine step whose product_recommendations
    entry carries a real formulation_id from the synthetic catalog and
    a real SafetyDecision -- not empty, not a category-only
    placeholder, and not sourced from evaluate_offer() alone (which has
    no notion of a formulation_id or catalog rules_version at all)."""
    headers = await _signup_login_consent(client, "reco-pipeline-safe@test.com")
    await client.put(
        "/profile",
        json={
            "has_sensitive_skin": False, "experience_level": "intermediate", "max_routine_steps": 10,
            "is_pregnant": False, "is_nursing": False, "allergies": [], "avoid_ingredients": [],
        },
        headers=headers,
    )

    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode("ascii")
    resp = await client.post("/analyze", json={"image_base64": image_b64}, headers=headers)
    assert resp.status_code == 200
    plan = resp.json()["plan"]

    step = _find_step_with_recommendations(plan)
    assert step is not None, "expected at least one routine step (e.g. the always-present moisturizer step) to have a real product match"

    rec = step["product_recommendations"][0]
    assert rec["formulation_id"] is not None
    assert rec["safety_status"] in ("SAFE", "RESTRICTED")
    assert rec["safety_decision"]["candidate_type"] == "product_formulation"
    assert rec["safety_decision"]["rules_version"]

    assert "routine_safety_decision" in plan["metadata"]
    assert plan["metadata"]["routine_safety_decision"]["candidate_type"] == "routine"


async def test_analyze_allergy_conflict_excludes_that_specific_product(client, synthetic_catalog):
    """A user allergic to fragrance must never receive the
    fragrance-containing formulation as a specific recommendation, even
    though "moisturizer" (its category) is itself category-level safe
    (fragrance isn't in product_safety.py's category-level profile for
    plain "moisturizer") -- this can only be caught by real per-
    ingredient, formulation-level evaluation, proving that path is what
    actually ran."""
    headers = await _signup_login_consent(client, "reco-pipeline-allergy@test.com")
    await client.put(
        "/profile",
        json={
            "has_sensitive_skin": False, "experience_level": "intermediate", "max_routine_steps": 10,
            "is_pregnant": False, "is_nursing": False, "allergies": ["fragrance"], "avoid_ingredients": [],
        },
        headers=headers,
    )

    image_b64 = base64.b64encode(GRACE_HOPPER_JPG.read_bytes()).decode("ascii")
    resp = await client.post("/analyze", json={"image_base64": image_b64}, headers=headers)
    assert resp.status_code == 200
    plan = resp.json()["plan"]

    all_formulation_ids = {
        rec["formulation_id"]
        for step in plan["am_routine"] + plan["pm_routine"]
        for rec in step["product_recommendations"]
    }
    assert str(synthetic_catalog["formulations"]["fragrance"]) not in all_formulation_ids
